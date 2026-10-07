"""Phase 5.5 manual, fail-closed operations and recovery controls.

This module never constructs an order payload and never retries submission.
It interprets immutable artifacts, the canonical ledger, and reconciliation
snapshots so an operator can choose the one safe next action.
"""
from __future__ import annotations

import hashlib
import json
import os
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
        identity = f"{client_order_id}|{approval_id}|{canary_id}"
        self.path = self.root / "operation_locks" / (hashlib.sha256(identity.encode()).hexdigest() + ".lock")
        self._owned = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"client_order_id": self.path.stem, "created_at_utc": self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")})
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
        related = [item[0] for item in reconciliations if link and item[0].get("client_order_id") == link]
        partial_quote = sum((Decimal(str(fill["quote_value_usdt"])) for item in related if item.get("reconciliation_state") == "partial" for fill in item.get("fills", [])), Decimal("0"))
        state, reasons, allowed = "SAFE_IDLE", [], ["prepare"]
        reconciliation_required = False
        production_defaults_block = not config.live_execution_enabled or config.kill_switch or config.order_submission not in {"implemented_disabled", "implemented"}
        if production_defaults_block:
            reasons.append("checked-in production safety defaults prohibit live execution")
        if remaining < Decimal("10"):
            state, allowed = "MONTHLY_CAP_REACHED", ["inspect", "audit"]
            reasons.append("fresh confirmed calendar-month budget is below V1 minimum")
        if attempt and (not outcome or outcome[0].get("outcome_category") != "confirmed_execution"):
            state, allowed, reconciliation_required = "RECONCILIATION_REQUIRED", ["reconcile-existing", "inspect", "audit"], True
            reasons.append("prior submission attempt prohibits new submission")
        if latest_reconciliation and latest_reconciliation[0].get("reconciliation_state") in {"active", "partial"}:
            state, allowed, reconciliation_required = "RECONCILIATION_REQUIRED", ["reconcile-existing", "inspect", "audit"], True
            reasons.append("authoritative order remains active or partially filled")
        elif manifest_data:
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
        chain: dict[str, Any] = {}
        for kind, timestamp in ((ArtifactType.DECISION, "created_at_utc"), (ArtifactType.ORDER_INTENT, "created_at_utc"), (ArtifactType.CANARY_MANIFEST, "prepared_at_utc"), (ArtifactType.LIVE_APPROVAL, "approved_at_utc"), (ArtifactType.ORDER_SUBMISSION_ATTEMPT, "created_at_utc"), (ArtifactType.ORDER_SUBMISSION_OUTCOME, "completed_at_utc"), (ArtifactType.SUBMISSION_RECONCILIATION, "reconciled_at_utc")):
            items = [item for item in self._items(kind) if item[0].get("run_id") == run_id or item[2].stem == run_id]
            chain[kind.value] = [{"path": str(path), "schema_version": payload["schema_version"], "sha256": digest, "timestamp": payload.get(timestamp), "status": "valid"} for payload, digest, path in items]
        decisions = [item for item in self._items(ArtifactType.DECISION) if item[0].get("run_id") == run_id]
        decision_id = decisions[0][0].get("decision_id") if len(decisions) == 1 else None
        executions = [item.payload for item in read_executions(self.ledger_path) if decision_id and item.payload.get("decision_id") == decision_id]
        chain["execution"] = [{"execution_id": item.get("execution_id"), "status": item.get("status"), "ledger": "canonical"} for item in executions]
        identities = [entry for entries in chain.values() for entry in entries]
        return {"run_id": run_id, "chain": chain, "status": "complete" if identities else "missing", "integrity": "valid" if identities else "missing", "identity_consistent": len(decisions) <= 1}

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
