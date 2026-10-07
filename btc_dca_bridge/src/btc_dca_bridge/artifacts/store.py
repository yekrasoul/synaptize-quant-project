"""Atomic, append-only filesystem persistence for canonical artifacts.

Publication uses ``os.link`` from a fully-fsynced temporary file into the
target directory.  On macOS and Linux, linking to an existing name atomically
fails with EEXIST, unlike ``os.replace`` which would violate immutability.
Hard links must be supported by the target filesystem; otherwise the write
fails closed with ``PERSISTENCE_IO_ERROR``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from ..errors import (
    ArtifactAlreadyExistsError,
    ArtifactCorruptError,
    ArtifactInvalidError,
    ArtifactNotFoundError,
    ArtifactSchemaValidationError,
    InvalidArtifactPathError,
    PersistenceIOError,
    SchemaValidationError,
)
from ..paths import DATA_PATH
from ..schemas import validate_artifact, validate_live_approval


class ArtifactType(str, Enum):
    MARKET = "market"
    SENTIMENT = "sentiment"
    DECISION = "decision"
    RUN = "run"
    ORDER_INTENT = "order_intent"
    SAFETY_VALIDATION = "safety_validation"
    ORDER_SUBMISSION_ATTEMPT = "order_submission_attempt"
    ORDER_SUBMISSION_OUTCOME = "order_submission_outcome"
    CANARY_MANIFEST = "canary_manifest"
    LIVE_APPROVAL = "live_approval"
    SUBMISSION_RECONCILIATION = "submission_reconciliation"


_DIRECTORIES = {
    ArtifactType.MARKET: "market",
    ArtifactType.SENTIMENT: "sentiment",
    ArtifactType.DECISION: "decisions",
    ArtifactType.RUN: "runs",
    ArtifactType.ORDER_INTENT: "order_intents",
    ArtifactType.SAFETY_VALIDATION: "safety_validations",
    ArtifactType.ORDER_SUBMISSION_ATTEMPT: "order_submission_attempts",
    ArtifactType.ORDER_SUBMISSION_OUTCOME: "order_submission_outcomes",
    ArtifactType.CANARY_MANIFEST: "canary_manifests",
    ArtifactType.LIVE_APPROVAL: "live_approvals",
    ArtifactType.SUBMISSION_RECONCILIATION: "submission_reconciliations",
}
_SCHEMAS = {
    ArtifactType.MARKET: "market_snapshot",
    ArtifactType.SENTIMENT: "sentiment_snapshot",
    ArtifactType.DECISION: "decision",
    ArtifactType.RUN: "shadow_run",
    ArtifactType.ORDER_INTENT: "order_intent",
    ArtifactType.SAFETY_VALIDATION: "safety_validation",
    ArtifactType.ORDER_SUBMISSION_ATTEMPT: "order_submission_attempt",
    ArtifactType.ORDER_SUBMISSION_OUTCOME: "order_submission_outcome",
    ArtifactType.CANARY_MANIFEST: "canary_manifest",
    ArtifactType.LIVE_APPROVAL: "live_approval",
    ArtifactType.SUBMISSION_RECONCILIATION: "submission_reconciliation",
}
_TIMESTAMPS = {
    ArtifactType.MARKET: "captured_at_utc",
    ArtifactType.SENTIMENT: "retrieved_at_utc",
    ArtifactType.DECISION: "created_at_utc",
    ArtifactType.RUN: "completed_at_utc",
    ArtifactType.ORDER_INTENT: "created_at_utc",
    ArtifactType.SAFETY_VALIDATION: "checked_at_utc",
    ArtifactType.ORDER_SUBMISSION_ATTEMPT: "created_at_utc",
    ArtifactType.ORDER_SUBMISSION_OUTCOME: "completed_at_utc",
    ArtifactType.CANARY_MANIFEST: "prepared_at_utc",
    ArtifactType.LIVE_APPROVAL: "approved_at_utc",
    ArtifactType.SUBMISSION_RECONCILIATION: "reconciled_at_utc",
}
_RUN_ID = re.compile(r"^run_\d{8}T\d{6}Z_[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


@dataclass(frozen=True)
class ArtifactReceipt:
    artifact_type: ArtifactType
    run_id: str
    path: Path
    schema_version: str
    sha256: str
    byte_length: int
    created: bool = True


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise InvalidArtifactPathError(f"{label} must be a UTC-aware datetime")
    return value.astimezone(UTC)


def make_run_id(run_at_utc: datetime, stable_suffix: str) -> str:
    """Create a deterministic, filesystem-safe ID from injected run identity."""
    instant = _utc(run_at_utc, "run_at_utc")
    candidate = f"run_{instant.strftime('%Y%m%dT%H%M%SZ')}_{stable_suffix}"
    _validate_run_id(candidate)
    return candidate


def _validate_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise InvalidArtifactPathError("run_id must be a canonical filesystem-safe run identifier")
    return run_id


def _artifact_type(value: ArtifactType | str) -> ArtifactType:
    try:
        return ArtifactType(value)
    except (TypeError, ValueError) as exc:
        raise InvalidArtifactPathError("unsupported artifact type") from exc


def _parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ArtifactInvalidError(f"{label} must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArtifactInvalidError(f"{label} must be an RFC 3339 UTC timestamp") from exc
    return _utc(parsed, label)


def _canonical_bytes(artifact: Mapping[str, Any]) -> bytes:
    try:
        # UTF-8, lexical keys, compact separators, no NaN, exactly one LF.
        return (json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArtifactInvalidError("artifact is not canonically JSON serializable") from exc


class ArtifactStore:
    """Persist and read exact immutable artifacts below an injectable root."""

    def __init__(self, root: Path = DATA_PATH) -> None:
        self.root = Path(root)

    def persist(self, artifact_type: ArtifactType | str, artifact: Mapping[str, Any] | Any, *, run_id: str) -> ArtifactReceipt:
        kind = _artifact_type(artifact_type)
        safe_run_id = _validate_run_id(run_id)
        payload = self._payload(artifact)
        self._validate(kind, payload)
        timestamp = _parse_timestamp(payload.get(_TIMESTAMPS[kind]), _TIMESTAMPS[kind])
        directory = self._directory(kind, timestamp)
        final = directory / f"{safe_run_id}.json"
        digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
        digest_final = directory / f"{safe_run_id}.json.sha256"
        content = _canonical_bytes(payload)
        if final.exists() or digest_final.exists():
            raise ArtifactAlreadyExistsError(f"artifact already exists for {kind.value}/{safe_run_id}")
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PersistenceIOError(f"cannot create artifact directory: {exc}") from exc
        return self._publish(kind, safe_run_id, payload, content, digest, final, digest_final)

    def read(self, artifact_type: ArtifactType | str, *, run_id: str, artifact_date_utc: datetime) -> dict[str, Any]:
        kind = _artifact_type(artifact_type)
        safe_run_id = _validate_run_id(run_id)
        directory = self._directory(kind, _utc(artifact_date_utc, "artifact_date_utc"))
        final = directory / f"{safe_run_id}.json"
        digest_path = directory / f"{safe_run_id}.json.sha256"
        try:
            content = final.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(f"artifact not found for {kind.value}/{safe_run_id}") from exc
        except OSError as exc:
            raise PersistenceIOError(f"cannot read artifact: {exc}") from exc
        try:
            expected = digest_path.read_text(encoding="ascii")
        except FileNotFoundError as exc:
            raise ArtifactCorruptError("artifact digest sidecar is missing") from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise ArtifactCorruptError("artifact digest sidecar cannot be read") from exc
        actual = hashlib.sha256(content).hexdigest()
        if expected != f"{actual}\n":
            raise ArtifactCorruptError("artifact digest does not match canonical bytes")
        try:
            decoded = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactCorruptError("artifact is not valid UTF-8 JSON") from exc
        if not isinstance(decoded, dict):
            raise ArtifactCorruptError("artifact root must be a JSON object")
        self._validate(kind, decoded, corrupt=True)
        timestamp = _parse_timestamp(decoded.get(_TIMESTAMPS[kind]), _TIMESTAMPS[kind])
        if self._directory(kind, timestamp) != directory:
            raise ArtifactCorruptError("artifact timestamp does not match its UTC directory")
        if _canonical_bytes(decoded) != content:
            raise ArtifactCorruptError("artifact does not use canonical serialization")
        return decoded

    def find_completed_run(self, *, run_id: str) -> dict[str, Any]:
        """Find one exact completed manifest by safe identity across UTC dates."""
        safe_run_id = _validate_run_id(run_id)
        run_root = self.root / _DIRECTORIES[ArtifactType.RUN]
        matches = sorted(run_root.glob(f"*/*/*/{safe_run_id}.json"))
        if not matches:
            raise ArtifactNotFoundError(f"completed run not found: {safe_run_id}")
        if len(matches) != 1:
            raise ArtifactCorruptError(
                f"multiple completed manifests exist for run identity: {safe_run_id}"
            )
        path = matches[0]
        try:
            year, month, day = (int(part) for part in path.parts[-4:-1])
            artifact_date = datetime(year, month, day, tzinfo=UTC)
        except (TypeError, ValueError) as exc:
            raise ArtifactCorruptError("completed run has an invalid UTC directory") from exc
        return self.read(
            ArtifactType.RUN,
            run_id=safe_run_id,
            artifact_date_utc=artifact_date,
        )

    def has_submission_attempt(self, decision_id: str, client_order_id: str) -> bool:
        """Conservatively detect prior prepared evidence before a POST."""
        return self.has_submission_artifact(decision_id, client_order_id, kinds=(ArtifactType.ORDER_SUBMISSION_ATTEMPT,))

    def find_artifact(self, artifact_type: ArtifactType | str, *, identity_field: str, identity_value: str) -> tuple[dict[str, Any], str]:
        """Read exactly one immutable artifact and return its verified digest."""
        kind = _artifact_type(artifact_type)
        root = self.root / _DIRECTORIES[kind]
        matches: list[Path] = []
        for path in root.glob("*/*/*/*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArtifactCorruptError(f"cannot inspect {kind.value} artifact") from exc
            if isinstance(raw, dict) and raw.get(identity_field) == identity_value:
                matches.append(path)
        if not matches:
            raise ArtifactNotFoundError(f"artifact {kind.value} {identity_value} not found")
        if len(matches) != 1:
            raise ArtifactCorruptError(f"multiple immutable {kind.value} artifacts match {identity_value}")
        parts = matches[0].parts
        try:
            artifact_date = datetime(int(parts[-4]), int(parts[-3]), int(parts[-2]), tzinfo=UTC)
        except (TypeError, ValueError, IndexError) as exc:
            raise ArtifactCorruptError("artifact directory date is invalid") from exc
        payload = self.read(kind, run_id=matches[0].stem, artifact_date_utc=artifact_date)
        digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
        return payload, digest

    def has_submission_attempt_identity(self, *, approval_id: str, canary_id: str, decision_id: str, client_order_id: str) -> bool:
        root = self.root / _DIRECTORIES[ArtifactType.ORDER_SUBMISSION_ATTEMPT]
        for path in root.glob("*/*/*/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return True
            if isinstance(payload, dict) and all(payload.get(k) == v for k, v in (("approval_id", approval_id), ("canary_id", canary_id), ("decision_id", decision_id), ("client_order_id", client_order_id))):
                return True
        return False

    def has_submission_artifact(self, decision_id: str, client_order_id: str, *, kinds: tuple[ArtifactType, ...] | None = None) -> bool:
        """Conservatively detect any prior immutable submission evidence."""
        kinds = kinds or (ArtifactType.ORDER_SUBMISSION_ATTEMPT, ArtifactType.ORDER_SUBMISSION_OUTCOME)
        for kind in kinds:
            root = self.root / _DIRECTORIES[kind]
            for path in root.glob("*/*/*/*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    return True
                if isinstance(payload, dict) and payload.get("decision_id") == decision_id and payload.get("client_order_id") == client_order_id:
                    return True
        return False

    def submission_reconciliations(self, *, decision_id: str, canary_id: str, client_order_id: str, order_id: str) -> tuple[dict[str, Any], ...]:
        """Return all digest-verified immutable reconciliation snapshots for one order."""
        kind = ArtifactType.SUBMISSION_RECONCILIATION
        matches: list[dict[str, Any]] = []
        for path in sorted((self.root / _DIRECTORIES[kind]).glob("*/*/*/*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArtifactCorruptError("cannot inspect submission reconciliation artifact") from exc
            if not isinstance(raw, dict):
                raise ArtifactCorruptError("submission reconciliation artifact is malformed")
            identity = (raw.get("decision_id"), raw.get("canary_id"), raw.get("client_order_id"), raw.get("order_id"))
            if identity != (decision_id, canary_id, client_order_id, order_id):
                continue
            try:
                year, month, day = (int(part) for part in path.parts[-4:-1])
            except (TypeError, ValueError) as exc:
                raise ArtifactCorruptError("submission reconciliation artifact directory date is invalid") from exc
            matches.append(self.read(kind, run_id=path.stem, artifact_date_utc=datetime(year, month, day, tzinfo=UTC)))
        return tuple(matches)

    def _payload(self, artifact: Mapping[str, Any] | Any) -> dict[str, Any]:
        candidate = artifact.to_dict() if hasattr(artifact, "to_dict") else artifact
        if not isinstance(candidate, Mapping):
            raise ArtifactInvalidError("artifact must be a mapping or expose to_dict()")
        return dict(candidate)

    def _validate(self, kind: ArtifactType, payload: dict[str, Any], *, corrupt: bool = False) -> None:
        try:
            if kind is ArtifactType.LIVE_APPROVAL:
                validate_live_approval(payload)
            else:
                validate_artifact(_SCHEMAS[kind], payload)
            if kind is ArtifactType.RUN:
                self._validate_run_manifest_identity(payload)
        except (SchemaValidationError, ValueError) as exc:
            error = ArtifactCorruptError if corrupt else ArtifactSchemaValidationError
            raise error(str(exc)) from exc

    @staticmethod
    def _validate_run_manifest_identity(payload: dict[str, Any]) -> None:
        run_id = payload["run_id"]
        completed = _parse_timestamp(payload["completed_at_utc"], "completed_at_utc")
        if payload["started_at_utc"] != payload["completed_at_utc"]:
            raise ValueError("shadow run manifest must use one canonical run instant")
        date_path = completed.strftime("%Y/%m/%d")
        for field, directory in (
            ("market_artifact", "market"),
            ("sentiment_artifact", "sentiment"),
            ("decision_artifact", "decisions"),
        ):
            expected = f"{directory}/{date_path}/{run_id}.json"
            if payload[field]["path"] != expected:
                raise ValueError(f"{field} path does not match run identity and UTC date")

    def _directory(self, kind: ArtifactType, timestamp: datetime) -> Path:
        timestamp = _utc(timestamp, "artifact timestamp")
        return self.root / _DIRECTORIES[kind] / timestamp.strftime("%Y") / timestamp.strftime("%m") / timestamp.strftime("%d")

    def _publish(self, kind: ArtifactType, run_id: str, payload: dict[str, Any], content: bytes, digest: str, final: Path, digest_final: Path) -> ArtifactReceipt:
        data_temp: Path | None = None
        digest_temp: Path | None = None
        sidecar_linked = False
        try:
            data_temp = self._write_temp(final.parent, run_id, content)
            digest_temp = self._write_temp(final.parent, run_id, f"{digest}\n".encode("ascii"))
            self._link_no_clobber(digest_temp, digest_final)
            sidecar_linked = True
            self._link_no_clobber(data_temp, final)
            self._fsync_directory(final.parent)
        except ArtifactAlreadyExistsError:
            if sidecar_linked:
                self._unlink_quietly(digest_final)
            raise
        except OSError as exc:
            if sidecar_linked:
                self._unlink_quietly(digest_final)
            raise PersistenceIOError(f"atomic artifact publication failed: {exc}") from exc
        finally:
            if data_temp is not None:
                self._unlink_quietly(data_temp)
            if digest_temp is not None:
                self._unlink_quietly(digest_temp)
        return ArtifactReceipt(kind, run_id, final, str(payload["schema_version"]), digest, len(content))

    @staticmethod
    def _write_temp(directory: Path, run_id: str, content: bytes) -> Path:
        fd, raw_path = tempfile.mkstemp(prefix=f".{run_id}.", suffix=".tmp", dir=directory)
        path = Path(raw_path)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        return path

    @staticmethod
    def _link_no_clobber(source: Path, destination: Path) -> None:
        try:
            os.link(source, destination)
        except FileExistsError as exc:
            raise ArtifactAlreadyExistsError(f"artifact target already exists: {destination.name}") from exc

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError as exc:
            raise PersistenceIOError(f"cannot open artifact directory for fsync: {exc}") from exc
        try:
            os.fsync(descriptor)
        except OSError as exc:
            # APFS and Linux filesystems used by supported environments expose
            # directory fsync. Unsupported filesystems fail closed.
            raise PersistenceIOError(f"cannot fsync artifact directory: {exc}") from exc
        finally:
            os.close(descriptor)
