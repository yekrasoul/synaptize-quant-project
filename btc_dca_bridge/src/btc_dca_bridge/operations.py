"""Phase 5.5 manual, fail-closed operations and recovery controls.

This module never constructs an order payload and never retries submission.
It interprets immutable artifacts, the canonical ledger, and reconciliation
snapshots so an operator can choose the one safe next action.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .artifacts import ArtifactStore, ArtifactType
from .config import load_execution_config, load_strategy_config
from .errors import ArtifactCorruptError, ArtifactNotFoundError
from .ledger import confirmed_executions, executions_for_month, read_executions


EXIT_OK, EXIT_BLOCKED, EXIT_RECONCILIATION, EXIT_UNAVAILABLE, EXIT_CORRUPT = 0, 2, 3, 4, 5
SOURCE = "Bybit private order/fill reconciliation"


class OperationLockError(ValueError): pass


class OperationLock:
    """Atomic per-identity local lock. Stale ownership needs explicit recovery."""
    def __init__(self, root: Path, *, client_order_id: str, approval_id: str, canary_id: str, now: Callable[[], datetime]) -> None:
        self.root, self.now = Path(root), now
        self.client_order_id, self.approval_id, self.canary_id = client_order_id, approval_id, canary_id
        identity = f"{client_order_id}|{approval_id}|{canary_id}"
        self.path = self.root / "operation_locks" / (hashlib.sha256(identity.encode()).hexdigest() + ".lock")
        self._owned = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "hostname": socket.gethostname(), "created_at_utc": self.now().astimezone(UTC).isoformat().replace("+00:00", "Z"), "client_order_id": self.client_order_id, "approval_id": self.approval_id, "canary_id": self.canary_id}, sort_keys=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise OperationLockError("operation lock already exists; reconcile or explicitly recover stale ownership") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush(); os.fsync(handle.fileno())
            self._owned = True
        except BaseException:
            try: self.path.unlink()
            except FileNotFoundError: pass
            raise

    def release(self) -> None:
        if self._owned:
            self.path.unlink(missing_ok=True)
            self._owned = False

    def recover_stale(self, *, max_age: timedelta = timedelta(minutes=15)) -> bool:
        """Explicitly clear only a provably stale crash residue; never steal a live lock."""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(str(payload["created_at_utc"]).replace("Z", "+00:00"))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise OperationLockError("operation lock ownership is ambiguous") from exc
        if created.tzinfo is None or self.now().astimezone(UTC) - created.astimezone(UTC) <= max_age:
            raise OperationLockError("operation lock is not provably stale")
        if payload.get("hostname") != socket.gethostname():
            raise OperationLockError("ambiguous ownership: lock belongs to another host")
        try:
            pid = int(payload["pid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OperationLockError("ambiguous ownership: malformed PID") from exc
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise OperationLockError("ambiguous ownership: PID liveness unresolved") from exc
        else:
            raise OperationLockError("lock owner is still alive")
        self.path.unlink()
        return True


@dataclass(frozen=True)
class OperationsSnapshot:
    state: str
    allowed_actions: tuple[str, ...]
    blocked_actions: tuple[str, ...]
    reasons: tuple[str, ...]
    artifact_ids: Mapping[str, str | None]
    monthly_budget: Mapping[str, str]
    reconciliation_required: bool
    operational_exposure: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "allowed_actions": list(self.allowed_actions), "blocked_actions": list(self.blocked_actions), "reasons": list(self.reasons), "artifact_ids": dict(self.artifact_ids), "monthly_budget": dict(self.monthly_budget), "reconciliation_required": self.reconciliation_required, "operational_exposure": dict(self.operational_exposure)}


class OperationsService:
    def __init__(self, *, data_root: Path, ledger_path: Path, now: Callable[[], datetime] | None = None) -> None:
        self.store = ArtifactStore(data_root)
        self.data_root, self.ledger_path = Path(data_root), Path(ledger_path)
        self.now = now or (lambda: datetime.now(UTC))

    def _items(self, kind: ArtifactType) -> list[tuple[dict[str, Any], str, Path]]:
        directories = {ArtifactType.DECISION: "decisions", ArtifactType.ORDER_INTENT: "order_intents", ArtifactType.CANARY_MANIFEST: "canary_manifests", ArtifactType.LIVE_APPROVAL: "live_approvals", ArtifactType.ORDER_SUBMISSION_ATTEMPT: "order_submission_attempts", ArtifactType.ORDER_SUBMISSION_OUTCOME: "order_submission_outcomes", ArtifactType.SUBMISSION_RECONCILIATION: "submission_reconciliations"}
        directory = self.data_root / directories[kind]
        result: list[tuple[dict[str, Any], str, Path]] = []
        for path in sorted(directory.glob("*/*/*/*.json")):
            try:
                year, month, day = (int(value) for value in path.parts[-4:-1])
                payload = self.store.read(kind, run_id=path.stem, artifact_date_utc=datetime(year, month, day, tzinfo=UTC))
                digest = hashlib.sha256((json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
            except Exception as exc:
                raise ArtifactCorruptError(f"cannot validate {kind.value} artifact {path}") from exc
            result.append((payload, digest, path))
        return result

    @staticmethod
    def _latest(items: list[tuple[dict[str, Any], str, Path]], timestamp: str, run_id: str | None) -> tuple[dict[str, Any], str, Path] | None:
        if run_id is not None:
            items = [item for item in items if item[0].get("run_id") == run_id or item[2].stem == run_id]
        return max(items, key=lambda item: str(item[0].get(timestamp, "")), default=None)

    def snapshot(self, *, run_id: str | None = None) -> OperationsSnapshot:
        config, strategy = load_execution_config(), load_strategy_config()
        month = self.now().astimezone(UTC).strftime("%Y-%m")
        ledger = read_executions(self.ledger_path)
        spent = sum((Decimal(str(item.payload["executed_usd"])) for item in executions_for_month(ledger, month)), Decimal("0"))
        remaining = strategy.monthly_cap_usd - spent
        decision = self._latest(self._items(ArtifactType.DECISION), "created_at_utc", run_id)
        manifest = self._latest(self._items(ArtifactType.CANARY_MANIFEST), "prepared_at_utc", run_id)
        approval = self._latest(self._items(ArtifactType.LIVE_APPROVAL), "approved_at_utc", run_id)
        attempt = self._latest(self._items(ArtifactType.ORDER_SUBMISSION_ATTEMPT), "created_at_utc", run_id)
        outcome = self._latest(self._items(ArtifactType.ORDER_SUBMISSION_OUTCOME), "completed_at_utc", run_id)
        reconciliations = self._items(ArtifactType.SUBMISSION_RECONCILIATION)
        latest_reconciliation = self._latest(reconciliations, "reconciled_at_utc", run_id)
        manifest_data = manifest[0] if manifest else None
        link = manifest_data.get("client_order_id") if manifest_data else None
        confirmed_for_identity = any(item.payload.get("decision_id") == (manifest_data or {}).get("decision_id") or (link is not None and item.payload.get("order_link_id") == link) for item in confirmed_executions(ledger))
        related = [item[0] for item in reconciliations if link and item[0].get("client_order_id") == link]
        partial_fills: dict[str, dict[str, Any]] = {}
        fill_fields = ("order_id", "order_link_id", "quantity_btc", "quote_value_usdt", "average_price_usdt", "executed_at_utc", "fee", "fee_asset", "category", "symbol")
        for item in related:
            if item.get("reconciliation_state") != "partial":
                continue
            for fill in item.get("fills", []):
                exec_id = str(fill.get("exec_id", ""))
                if not exec_id:
                    raise ArtifactCorruptError("partial reconciliation contains fill without execId")
                prior = partial_fills.get(exec_id)
                if prior is not None and any(str(prior.get(field)) != str(fill.get(field)) for field in fill_fields):
                    raise ArtifactCorruptError(f"conflicting partial fill evidence for execId {exec_id}")
                partial_fills[exec_id] = fill
        partial_quote = sum((Decimal(str(fill["quote_value_usdt"])) for fill in partial_fills.values()), Decimal("0"))
        state, reasons, allowed = "SAFE_IDLE", [], ["prepare"]
        reconciliation_required = False
        production_defaults_block = not config.live_execution_enabled or config.kill_switch or config.order_submission not in {"implemented_disabled", "implemented"}
        if production_defaults_block:
            reasons.append("checked-in production safety defaults prohibit live execution")
        if remaining < Decimal("10"):
            state, allowed = "MONTHLY_CAP_REACHED", ["inspect", "audit"]
            reasons.append("fresh confirmed calendar-month budget is below V1 minimum")
        if confirmed_for_identity:
            state, allowed = "SAFE_IDLE", ["prepare", "inspect", "audit"]
            reasons.append("authoritative execution is already recorded in the canonical ledger")
        elif attempt and (not outcome or outcome[0].get("outcome_category") != "confirmed_execution"):
            state, allowed, reconciliation_required = "RECONCILIATION_REQUIRED", ["reconcile-existing", "inspect", "audit"], True
            reasons.append("prior submission attempt prohibits new submission")
        if not confirmed_for_identity and latest_reconciliation and latest_reconciliation[0].get("reconciliation_state") in {"active", "partial"}:
            state, allowed, reconciliation_required = "RECONCILIATION_REQUIRED", ["reconcile-existing", "inspect", "audit"], True
            reasons.append("authoritative order remains active or partially filled")
        elif manifest_data and not confirmed_for_identity:
            expires = datetime.fromisoformat(str(manifest_data["expires_at_utc"]).replace("Z", "+00:00"))
            if expires <= self.now().astimezone(UTC):
                state, allowed = "BLOCKED", ["prepare", "inspect", "audit"]
                reasons.append("latest canary manifest is expired")
            elif approval:
                approval_data = approval[0]
                approval_expires = datetime.fromisoformat(str(approval_data["expires_at_utc"]).replace("Z", "+00:00"))
                if approval_expires > self.now().astimezone(UTC) and approval_data.get("canary_id") == manifest_data.get("canary_id") and not production_defaults_block:
                    state, allowed = "READY_FOR_MANUAL_EXECUTION", ["canary-execute", "inspect", "audit"]
                else:
                    state, allowed = "BLOCKED", ["inspect", "audit"]
                    reasons.append("manual execution remains disabled by checked-in production safety defaults")
            else:
                state, allowed = "READY_FOR_MANUAL_APPROVAL", ["approve", "inspect", "audit"]
        artifacts = {"decision_id": manifest_data.get("decision_id") if manifest_data else (decision[0].get("decision_id") if decision else None), "decision_status": decision[0].get("status") if decision else None, "canary_id": manifest_data.get("canary_id") if manifest_data else None, "canary_status": manifest_data.get("canary_status") if manifest_data else None, "manifest_expires_at_utc": manifest_data.get("expires_at_utc") if manifest_data else None, "approval_id": approval[0].get("approval_id") if approval else None, "approval_expires_at_utc": approval[0].get("expires_at_utc") if approval else None, "client_order_id": link, "attempt": "present" if attempt else None, "outcome": outcome[0].get("outcome_category") if outcome else None, "reconciliation": latest_reconciliation[0].get("reconciliation_state") if latest_reconciliation else None}
        exposure = {"confirmed_ledger_spend_usdt": str(spent), "known_unresolved_partial_quote_usdt": str(partial_quote), "remaining_confirmed_budget_usdt": str(remaining), "potential_effective_remaining_budget_usdt": str(remaining - partial_quote)}
        return OperationsSnapshot(state, tuple(allowed), ("new-submission",) if reconciliation_required else (), tuple(reasons), artifacts, {"calendar_month": month, "confirmed_spend_usdt": str(spent), "remaining_usdt": str(remaining)}, reconciliation_required, exposure)

    def audit_run(self, run_id: str) -> dict[str, Any]:
        kinds = ((ArtifactType.DECISION, "created_at_utc"), (ArtifactType.ORDER_INTENT, "created_at_utc"), (ArtifactType.CANARY_MANIFEST, "prepared_at_utc"), (ArtifactType.LIVE_APPROVAL, "approved_at_utc"), (ArtifactType.ORDER_SUBMISSION_ATTEMPT, "created_at_utc"), (ArtifactType.ORDER_SUBMISSION_OUTCOME, "completed_at_utc"), (ArtifactType.SUBMISSION_RECONCILIATION, "reconciled_at_utc"))
        all_items = {kind: self._items(kind) for kind, _ in kinds}
        roots = [item for item in all_items[ArtifactType.DECISION] if item[2].stem == run_id or item[0].get("run_id") == run_id]
        problems: list[str] = []
        if len(roots) != 1:
            return {"root_run_id": run_id, "related_run_ids": [], "status": "invalid", "integrity": "missing_or_inconsistent", "identity_consistent": False, "transition_consistent": False, "artifact_chain": {}, "ledger_execution": [], "problems": ["root Decision is missing or duplicated"]}
        decision = roots[0][0]
        identity = {"decision_id": decision.get("decision_id")}
        chain: dict[str, list[dict[str, Any]]] = {}
        def linked(payload: Mapping[str, Any]) -> bool:
            return any(value is not None and payload.get(key) == value for key, value in identity.items())
        for kind, timestamp in kinds:
            candidates = [item for item in all_items[kind] if item[2].stem == run_id or linked(item[0])]
            chain[kind.value] = [{"path": str(path), "schema_version": payload["schema_version"], "sha256": digest, "timestamp": payload.get(timestamp), "run_id": payload.get("run_id") or path.stem, "identity": {key: payload.get(key) for key in ("decision_id", "order_intent_id", "canary_id", "approval_id", "client_order_id", "order_id") if key in payload}, "status": "valid"} for payload, digest, path in candidates]
            for payload, _, _ in candidates:
                for key, value in payload.items():
                    if key.endswith("_id") and value and key in identity and value != identity[key]:
                        problems.append(f"identity mismatch in {kind.value}: {key}")
                identity.update({key: payload[key] for key in ("order_intent_id", "canary_id", "approval_id", "client_order_id", "order_id") if payload.get(key) and key not in identity})
        def first_payload(name: str) -> Mapping[str, Any] | None:
            entries = chain.get(name, [])
            if not entries:
                return None
            path = Path(entries[0]["path"])
            kind = next(kind for kind, _ in kinds if kind.value == name)
            year, month, day = (int(value) for value in path.parts[-4:-1])
            return self.store.read(kind, run_id=path.stem, artifact_date_utc=datetime(year, month, day, tzinfo=UTC))
        intent_payload = first_payload("order_intent")
        manifest_payload = first_payload("canary_manifest")
        approval_payload = first_payload("live_approval")
        attempt_payload = first_payload("order_submission_attempt")
        outcome_payload = first_payload("order_submission_outcome")
        reconciliation_payload = first_payload("submission_reconciliation")
        if intent_payload and any(intent_payload.get(key) != decision.get(key) for key in ("strategy_id", "strategy_version")):
            problems.append("Decision to OrderIntent strategy identity mismatch")
        if intent_payload and manifest_payload and any(intent_payload.get(key) != manifest_payload.get(key) for key in ("decision_id", "order_intent_id", "client_order_id", "exchange", "market_type", "symbol", "side")):
            problems.append("OrderIntent to CanaryManifest identity mismatch")
        if manifest_payload and approval_payload and any(manifest_payload.get(key) != approval_payload.get(key) for key in ("canary_id", "decision_id", "order_intent_id", "client_order_id", "approved_amount_usdt", "order_payload_fingerprint")):
            problems.append("CanaryManifest to LiveApproval identity mismatch")
        if approval_payload and attempt_payload and any(approval_payload.get(key) != attempt_payload.get(key) for key in ("approval_id", "canary_id", "decision_id", "order_intent_id", "client_order_id", "manifest_sha256", "approved_amount_usdt")):
            problems.append("LiveApproval to SubmissionAttempt identity mismatch")
        if attempt_payload and outcome_payload and any(attempt_payload.get(key) != outcome_payload.get(key) for key in ("decision_id", "order_intent_id", "client_order_id")):
            problems.append("SubmissionAttempt to SubmissionOutcome identity mismatch")
        if outcome_payload and reconciliation_payload and any(outcome_payload.get(key) != reconciliation_payload.get(key) for key in ("decision_id", "order_intent_id", "client_order_id")):
            problems.append("SubmissionOutcome to reconciliation identity mismatch")
        executions = [item.payload for item in read_executions(self.ledger_path) if item.payload.get("decision_id") == decision.get("decision_id")]
        ledger_execution = [{"execution_id": item.get("execution_id"), "status": item.get("status"), "ledger": "canonical"} for item in executions]
        required = ("decision", "order_intent", "canary_manifest", "live_approval", "order_submission_attempt", "order_submission_outcome", "submission_reconciliation")
        missing = [name for name in required if not chain[name]]
        if missing: problems.append("missing artifacts: " + ",".join(missing))
        if any(item.get("outcome_category") == "confirmed_execution" for raw in all_items[ArtifactType.ORDER_SUBMISSION_OUTCOME] for item in [raw[0]] if linked(item)) and not ledger_execution:
            problems.append("confirmed outcome has no ledger execution")
        related_runs = sorted({entry["run_id"] for entries in chain.values() for entry in entries} | {run_id})
        valid = not problems and bool(ledger_execution)
        return {"root_run_id": run_id, "related_run_ids": related_runs, "status": "complete" if valid else "invalid", "integrity": "valid" if valid else "invalid", "identity_consistent": not problems, "transition_consistent": not problems, "artifact_chain": chain, "ledger_execution": ledger_execution, "problems": problems, "missing_artifacts": missing}

    def record_event(self, *, action: str, result: str, reason: str, run_id: str | None, artifact_ids: Mapping[str, str | None]) -> Path:
        """Publish a digest-addressed immutable manual-operator audit event."""
        now = self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        event = {"timestamp_utc": now, "actor_mode": "manual_operator", "run_id": run_id, "artifact_ids": dict(artifact_ids), "action": action, "result": result, "reason": reason}
        canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        correlation_id = hashlib.sha256(canonical.encode()).hexdigest()
        event["correlation_id"] = correlation_id
        content = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        directory = self.data_root / "operational_events" / self.now().astimezone(UTC).strftime("%Y/%m/%d")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{correlation_id}.json"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        return path

    def health(self) -> dict[str, Any]:
        try:
            config = load_execution_config()
            if config.live_execution_enabled or not config.kill_switch or config.order_submission != "not_implemented":
                return {"status": "BLOCKED", "reason": "unsafe execution configuration is enabled"}
            snapshot = self.snapshot()
            inspected = {kind: self._items(kind) for kind in (ArtifactType.CANARY_MANIFEST, ArtifactType.LIVE_APPROVAL, ArtifactType.ORDER_SUBMISSION_ATTEMPT, ArtifactType.ORDER_SUBMISSION_OUTCOME, ArtifactType.SUBMISSION_RECONCILIATION)}
            for kind, field in ((ArtifactType.CANARY_MANIFEST, "canary_id"), (ArtifactType.LIVE_APPROVAL, "approval_id")):
                identities = [item[0].get(field) for item in inspected[kind]]
                if len(identities) != len(set(identities)):
                    return {"status": "CORRUPT", "reason": f"duplicate immutable {field}"}
        except ArtifactCorruptError as exc:
            return {"status": "CORRUPT", "reason": str(exc)}
        except Exception as exc:
            return {"status": "BLOCKED", "reason": str(exc)}
        status = "HEALTHY_WITH_UNRESOLVED_RECONCILIATION" if snapshot.reconciliation_required else "HEALTHY"
        return {"status": status, "snapshot": snapshot.to_dict()}
