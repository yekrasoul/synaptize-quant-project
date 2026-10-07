"""Read-only production status snapshots, diffs, and deduplicated alerts."""
from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .artifacts import ArtifactStore, ArtifactType, make_run_id
from .blocked_production import active_production_blockers, compare_contract_capabilities, contract_status
from .config import load_execution_config
from .errors import ArtifactAlreadyExistsError, ArtifactCorruptError, PersistenceIOError
from .ledger import executions_for_month, read_executions
from .operations import OperationsService
from .paths import DATA_PATH, LEDGER_PATH
from .private_bybit import BybitPrivateReadClient
from .production_evidence import AUTHORIZATION, ProductionEvidenceService, preauthorization_status
from .readiness import ProductionReadinessService
from .schemas import validate_artifact

SNAPSHOT_SCHEMA_VERSION = "6.0.0"
ALERT_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class ProductionStatusSnapshot:
    schema_version: str
    snapshot_id: str
    captured_at_utc: str
    repository_commit: str | None
    hostname: str
    software_health: str
    production_readiness: str
    preauthorization_status: str
    real_money_authorization: Mapping[str, Any]
    active_blockers: tuple[Mapping[str, Any], ...]
    blocker_ids: tuple[str, ...]
    contract_capabilities: Mapping[str, Any]
    account_identity_status: str
    account_mode_status: str
    credential_scope_status: str
    secret_hygiene_status: str
    spot_availability_status: str
    quote_unit_limit_status: str
    liabilities_status: str
    clock_status: str
    connectivity_status: str
    monthly_budget_status: str
    monthly_spent_usdt: str
    remaining_monthly_budget_usdt: str
    canonical_execution_count: int
    unresolved_operations: bool
    canonical_ledger_status: str
    evidence_status: str
    checked_in_execution_safety: Mapping[str, Any]
    overall_operator_state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "real_money_authorization": dict(self.real_money_authorization),
            "active_blockers": [dict(item) for item in self.active_blockers],
            "blocker_ids": list(self.blocker_ids),
            "contract_capabilities": dict(self.contract_capabilities),
            "checked_in_execution_safety": dict(self.checked_in_execution_safety),
        }


@dataclass(frozen=True)
class ProductionStatusDiff:
    classification: str
    changed_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"classification": self.classification, "changed_fields": list(self.changed_fields)}


def _json_digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_map(readiness: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(item.get("check_id")): item for item in readiness.get("checks", []) if isinstance(item, Mapping)}


def _check_status(checks: Mapping[str, Mapping[str, Any]], check_id: str) -> str:
    item = checks.get(check_id)
    return str(item.get("status", "UNAVAILABLE")) if item else "UNAVAILABLE"


def compare_status_snapshots(previous: Mapping[str, Any], current: Mapping[str, Any]) -> ProductionStatusDiff:
    """Classify factual changes. This function never changes readiness or policy."""
    excluded = {"snapshot_id", "captured_at_utc", "sha256"}
    changed_fields: list[str] = []
    for key in sorted((set(previous) | set(current)) - excluded):
        if key == "contract_capabilities":
            drift = compare_contract_capabilities(previous.get(key, {}), current.get(key, {}))
            if drift.result != "NO_CHANGE":
                changed_fields.append(key)
        elif key == "active_blockers":
            def stable_blockers(rows: Any) -> list[dict[str, Any]]:
                return [
                    {name: value for name, value in item.items() if name not in {"first_observed_at_utc", "last_observed_at_utc"}}
                    for item in rows if isinstance(item, Mapping)
                ]
            if stable_blockers(previous.get(key, [])) != stable_blockers(current.get(key, [])):
                changed_fields.append(key)
        elif previous.get(key) != current.get(key):
            changed_fields.append(key)
    changed = tuple(changed_fields)
    if not changed:
        return ProductionStatusDiff("NO_MATERIAL_CHANGE", ())
    new_safety = current.get("checked_in_execution_safety", {})
    if (
        current.get("overall_operator_state") == "CORRUPT"
        or current.get("canonical_ledger_status") == "CORRUPT"
        or current.get("evidence_status") in {"CORRUPT", "INVALID"}
        or current.get("account_identity_status") in {"MISMATCH", "INVALID"}
        or current.get("secret_hygiene_status") in {"FAIL", "CORRUPT"}
        or new_safety.get("live_execution_enabled") is not False
        or new_safety.get("kill_switch") is not True
        or new_safety.get("order_submission") != "not_implemented"
        or (not previous.get("unresolved_operations") and current.get("unresolved_operations"))
    ):
        return ProductionStatusDiff("SAFETY_REGRESSION", changed)
    if previous.get("unresolved_operations") and not current.get("unresolved_operations"):
        return ProductionStatusDiff("RECOVERY_PROGRESS", changed)
    capability_drift = compare_contract_capabilities(
        previous.get("contract_capabilities", {}), current.get("contract_capabilities", {})
    )
    if previous.get("blocker_ids") != current.get("blocker_ids") or capability_drift.result != "NO_CHANGE":
        return ProductionStatusDiff("EXTERNAL_DEPENDENCY_CHANGE", changed)
    attention_fields = {
        "account_identity_status", "account_mode_status", "credential_scope_status",
        "spot_availability_status", "quote_unit_limit_status", "liabilities_status",
        "clock_status", "connectivity_status", "evidence_status", "software_health",
    }
    degraded = {"FAIL", "UNAVAILABLE", "STALE", "CORRUPT", "INVALID", "BLOCKED", "ACCOUNT_IDENTITY_UNPROVEN"}
    if any(key in attention_fields and str(current.get(key, "")) in degraded for key in changed):
        return ProductionStatusDiff("ATTENTION_REQUIRED", changed)
    if "monthly_budget_status" in changed and current.get("monthly_budget_status") != "SAFE":
        return ProductionStatusDiff("ATTENTION_REQUIRED", changed)
    return ProductionStatusDiff("INFO_CHANGE", changed)


def alert_class_for(classification: str) -> str | None:
    return {
        "INFO_CHANGE": "INFO",
        "ATTENTION_REQUIRED": "WARNING",
        "SAFETY_REGRESSION": "CRITICAL",
        "RECOVERY_PROGRESS": "RECOVERY",
        "EXTERNAL_DEPENDENCY_CHANGE": "EXTERNAL_CHANGE",
    }.get(classification)


def format_production_status_alert(snapshot: Mapping[str, Any], diff: ProductionStatusDiff) -> str:
    blocker_ids = snapshot.get("blocker_ids") or []
    blocker = ", ".join(str(item) for item in blocker_ids) if blocker_ids else "none"
    lines = [
        "BTC DCA PRODUCTION STATUS",
        f"Alert: {alert_class_for(diff.classification) or 'INFO'} — {diff.classification}",
        f"State: {snapshot.get('overall_operator_state', 'ACTION_REQUIRED')}",
        f"Software: {snapshot.get('software_health', 'UNAVAILABLE')}",
        f"Readiness: {snapshot.get('production_readiness', 'UNAVAILABLE')}",
        f"Preauthorization: {snapshot.get('preauthorization_status', 'UNAVAILABLE')}",
        f"Blocker: {blocker}",
        "Authorization: NOT_AUTHORIZED",
        f"Changed: {', '.join(diff.changed_fields)}",
        "Action: Observe only; resolve reconciliation if required.",
    ]
    return "\n".join(lines)


def derive_transition_alert(
    previous: Mapping[str, Any],
    previous_sha256: str,
    current: Mapping[str, Any],
    current_sha256: str,
) -> dict[str, Any] | None:
    """Derive the one immutable alert implied by two persisted snapshots.

    The supplied digests must be the verified canonical ArtifactStore digests.
    No wall-clock, delivery, or mutable external state participates.
    """
    if not all(isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value) for value in (previous_sha256, current_sha256)):
        raise ValueError("transition snapshots require canonical SHA-256 digests")
    diff = compare_status_snapshots(previous, current)
    if diff.classification == "NO_MATERIAL_CHANGE":
        return None
    changed_fields = sorted(diff.changed_fields)
    dedup_key = hashlib.sha256("|".join((
        previous_sha256,
        current_sha256,
        diff.classification,
        ",".join(changed_fields),
    )).encode("utf-8")).hexdigest()
    return {
        "schema_version": ALERT_SCHEMA_VERSION,
        "alert_id": f"alert-{dedup_key}",
        "dedup_key": dedup_key,
        "created_at_utc": str(current["captured_at_utc"]),
        "previous_snapshot_id": str(previous["snapshot_id"]),
        "current_snapshot_id": str(current["snapshot_id"]),
        "classification": diff.classification,
        "alert_class": alert_class_for(diff.classification),
        "changed_fields": changed_fields,
        "message": format_production_status_alert(current, diff),
    }


class ProductionStatusService:
    def __init__(
        self,
        *,
        data_root: Path = DATA_PATH,
        ledger_path: Path = LEDGER_PATH,
        now: Callable[[], datetime] | None = None,
        readiness_factory: Callable[..., Any] = ProductionReadinessService,
        operations_factory: Callable[..., Any] = OperationsService,
        evidence_service_factory: Callable[..., Any] | None = None,
        preauthorization_evaluator: Callable[[Any], Mapping[str, Any]] = preauthorization_status,
        contract_provider: Callable[..., Mapping[str, Any]] = contract_status,
        blocker_provider: Callable[..., Any] = active_production_blockers,
        repo_probe: Callable[[], Mapping[str, Any]] | None = None,
        hostname: Callable[[], str] = socket.gethostname,
    ) -> None:
        self.data_root, self.ledger_path = Path(data_root), Path(ledger_path)
        self.now = now or (lambda: datetime.now(UTC))
        self.readiness_factory = readiness_factory
        self.operations_factory = operations_factory
        self.evidence_service_factory = evidence_service_factory or self._default_evidence_service
        self.preauthorization_evaluator = preauthorization_evaluator
        self.contract_provider = contract_provider
        self.blocker_provider = blocker_provider
        self.repo_probe = repo_probe or ProductionReadinessService._repo_status
        self.hostname = hostname
        self.store = ArtifactStore(self.data_root)

    def _default_evidence_service(self, **kwargs: Any) -> ProductionEvidenceService:
        return ProductionEvidenceService(**kwargs, client_factory=BybitPrivateReadClient.from_environment)

    def evaluate(self) -> ProductionStatusSnapshot:
        captured = self.now().astimezone(UTC)
        readiness_kwargs = {"data_root": self.data_root, "ledger_path": self.ledger_path, "now": self.now}
        operations_kwargs = {"data_root": self.data_root, "ledger_path": self.ledger_path, "now": self.now}
        readiness: Mapping[str, Any]
        health: Mapping[str, Any]
        ops_snapshot: Any
        evidence: Any
        preauth: Mapping[str, Any]
        local_error = False
        try:
            readiness = self.readiness_factory(**readiness_kwargs).evaluate()
        except Exception:
            readiness = {"status": "UNAVAILABLE", "checks": []}
            local_error = True
        try:
            operations = self.operations_factory(**operations_kwargs)
            health = operations.health()
            ops_snapshot = operations.snapshot()
        except Exception:
            health = {"status": "CORRUPT"}
            ops_snapshot = None
            local_error = True
        try:
            evidence = self.evidence_service_factory(data_root=self.data_root, ledger_path=self.ledger_path, now=self.now)
            preauth = self.preauthorization_evaluator(evidence)
            latest_evidence = evidence.latest()
            evidence_status = "MISSING" if latest_evidence is None else str(evidence.verify(str(latest_evidence["evidence_id"])).get("status", "INVALID"))
        except Exception:
            evidence, latest_evidence, preauth = None, None, {"status": "BLOCKED"}
            evidence_status, local_error = "UNAVAILABLE", True
        checks = _check_map(readiness)
        try:
            config = load_execution_config()
            safety = {
                "live_execution_enabled": config.live_execution_enabled,
                "kill_switch": config.kill_switch,
                "order_submission": config.order_submission,
            }
        except Exception:
            safety = {"live_execution_enabled": True, "kill_switch": False, "order_submission": "unknown"}
            local_error = True
        try:
            executions = read_executions(self.ledger_path)
            month = captured.strftime("%Y-%m")
            month_rows = executions_for_month(executions, month)
            spent = sum((Decimal(str(row.payload["executed_usd"])) for row in month_rows), Decimal("0"))
            remaining = Decimal("500") - spent
            ledger_status = "VALID"
            execution_count = len(executions)
        except Exception:
            spent, remaining, ledger_status, execution_count = Decimal("0"), Decimal("0"), "CORRUPT", 0
        try:
            health_status = str(health.get("status", "UNAVAILABLE"))
            blockers = tuple(item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in self.blocker_provider(now=captured))
            contract = dict(self.contract_provider(now=captured))
            capabilities = dict(contract.get("capabilities", {}))
            unresolved = bool(getattr(ops_snapshot, "reconciliation_required", False))
        except Exception:
            health_status, blockers, capabilities, unresolved = "BLOCKED", (), {}, False
            local_error = True
        readiness_status = str(readiness.get("status", "UNAVAILABLE"))
        preauth_status = str(preauth.get("status", "UNAVAILABLE"))
        external_ids = [str(item.get("blocker_id")) for item in blockers if item.get("external_dependency")]
        safe_config = safety == {"live_execution_enabled": False, "kill_switch": True, "order_submission": "not_implemented"}
        required_attention_checks = {
            "SECRET_HYGIENE", "BYBIT_CREDENTIAL_SCOPE", "BYBIT_ACCOUNT",
            "BYBIT_LIABILITIES", "BYBIT_SPOT_AVAILABLE_BALANCE", "CLOCK_SKEW", "PRODUCTION_CONNECTIVITY",
        }
        attention = local_error or not safe_config or health_status == "BLOCKED" or evidence_status not in {"VALID_NOT_READY", "VALID_READY_FOR_SEPARATE_AUTHORIZATION"} or remaining < Decimal("10") or any(
            _check_status(checks, check_id) != "PASS" for check_id in required_attention_checks
        )
        if ledger_status == "CORRUPT" or health_status == "CORRUPT" or evidence_status == "CORRUPT":
            overall = "CORRUPT"
        elif attention:
            overall = "ACTION_REQUIRED"
        elif unresolved:
            overall = "RECONCILIATION_REQUIRED"
        elif external_ids or health_status == "HEALTHY_BLOCKED_EXTERNAL_DEPENDENCY":
            overall = "EXTERNAL_DEPENDENCY_BLOCKED"
        elif readiness_status == "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION" and preauth_status == "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION":
            overall = "HEALTHY_OBSERVE_ONLY"
        elif readiness_status in {"NOT_READY", "UNAVAILABLE"}:
            overall = "ACTION_REQUIRED"
        else:
            overall = "HEALTHY_OBSERVE_ONLY"
        try:
            repo = self.repo_probe()
            commit = str(repo.get("commit")) if repo.get("commit") else None
        except Exception:
            commit, local_error = None, True
            if overall not in {"CORRUPT", "ACTION_REQUIRED"}:
                overall = "ACTION_REQUIRED"
        identity_status = str(latest_evidence.get("account_identity_status", "UNAVAILABLE")) if latest_evidence and evidence_status in {"VALID_NOT_READY", "VALID_READY_FOR_SEPARATE_AUTHORIZATION"} else "UNAVAILABLE"
        values: dict[str, Any] = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "captured_at_utc": captured.isoformat().replace("+00:00", "Z"),
            "repository_commit": commit,
            "hostname": self.hostname(),
            "software_health": health_status,
            "production_readiness": readiness_status if readiness_status in {"NOT_READY", "READY_FOR_OPERATOR_PREPARATION", "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION"} else "UNAVAILABLE",
            "preauthorization_status": preauth_status if preauth_status in {"BLOCKED", "READY_FOR_OPERATOR_PREPARATION", "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION"} else "UNAVAILABLE",
            "real_money_authorization": dict(AUTHORIZATION),
            "active_blockers": blockers,
            "blocker_ids": tuple(sorted(str(item.get("blocker_id")) for item in blockers)),
            "contract_capabilities": capabilities,
            "account_identity_status": identity_status,
            "account_mode_status": _check_status(checks, "BYBIT_ACCOUNT"),
            "credential_scope_status": _check_status(checks, "BYBIT_CREDENTIAL_SCOPE"),
            "secret_hygiene_status": _check_status(checks, "SECRET_HYGIENE"),
            "spot_availability_status": _check_status(checks, "BYBIT_SPOT_AVAILABLE_BALANCE"),
            "quote_unit_limit_status": str((readiness.get("quote_unit_limit_evidence") or {}).get("conclusion", "UNAVAILABLE")),
            "liabilities_status": _check_status(checks, "BYBIT_LIABILITIES"),
            "clock_status": _check_status(checks, "CLOCK_SKEW"),
            "connectivity_status": _check_status(checks, "PRODUCTION_CONNECTIVITY"),
            "monthly_budget_status": "INVALID" if ledger_status == "CORRUPT" else ("SAFE" if remaining >= Decimal("10") else "CAP_OR_MINIMUM_BLOCKED"),
            "monthly_spent_usdt": str(spent),
            "remaining_monthly_budget_usdt": str(remaining),
            "canonical_execution_count": execution_count,
            "unresolved_operations": unresolved,
            "canonical_ledger_status": ledger_status,
            "evidence_status": evidence_status,
            "checked_in_execution_safety": safety,
            "overall_operator_state": overall,
        }
        seed = hashlib.sha256(f"{values['captured_at_utc']}|{commit}|{_json_digest(values)}".encode()).hexdigest()[:20]
        values["snapshot_id"] = make_run_id(captured, f"prodstatus_{seed}")
        snapshot = ProductionStatusSnapshot(**values)
        validate_artifact("production_status", snapshot.to_dict())
        return snapshot

    def _read_snapshot_path(self, path: Path) -> tuple[dict[str, Any], str]:
        date = datetime(int(path.parts[-4]), int(path.parts[-3]), int(path.parts[-2]), tzinfo=UTC)
        payload = self.store.read(ArtifactType.PRODUCTION_STATUS, run_id=path.stem, artifact_date_utc=date)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return payload, digest

    def history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("history limit must be within 1..1000")
        root = self.data_root / "production_status"
        rows = [self._read_snapshot_path(path) for path in sorted(root.glob("*/*/*/*.json"))]
        rows.sort(key=lambda pair: (pair[0]["captured_at_utc"], pair[0]["snapshot_id"]), reverse=True)
        return [{**payload, "sha256": digest} for payload, digest in rows[:limit]]

    def _existing_alerts(self) -> dict[str, dict[str, Any]]:
        root = self.data_root / "production_status_alerts"
        alerts: dict[str, dict[str, Any]] = {}
        for path in sorted(root.glob("*/*/*/*.json")):
            date = datetime(int(path.parts[-4]), int(path.parts[-3]), int(path.parts[-2]), tzinfo=UTC)
            payload = self.store.read(ArtifactType.PRODUCTION_STATUS_ALERT, run_id=path.stem, artifact_date_utc=date)
            key = str(payload["dedup_key"])
            if key in alerts:
                raise ArtifactCorruptError("duplicate immutable production-status alert identity")
            alerts[key] = payload
        return alerts

    @staticmethod
    def _chronological_transitions(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        chronological = list(reversed(rows))
        return list(zip(chronological, chronological[1:]))

    def _persist_transition_alert(
        self,
        previous: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        expected = derive_transition_alert(previous, str(previous["sha256"]), current, str(current["sha256"]))
        if expected is None:
            return None
        alerts = self._existing_alerts()
        existing = alerts.get(expected["dedup_key"])
        if existing is not None:
            if existing != expected:
                raise ArtifactCorruptError("persisted production-status alert contradicts its immutable snapshots")
            return None
        created_at = datetime.fromisoformat(expected["created_at_utc"].replace("Z", "+00:00")).astimezone(UTC)
        alert_run_id = make_run_id(created_at, f"prodalert_{expected['dedup_key'][:20]}")
        try:
            receipt = self.store.persist(ArtifactType.PRODUCTION_STATUS_ALERT, expected, run_id=alert_run_id)
        except ArtifactAlreadyExistsError:
            # Concurrent collectors may race on the same deterministic path. A
            # read-back makes this idempotent; inconsistent bytes remain corruption.
            existing = self._existing_alerts().get(expected["dedup_key"])
            if existing != expected:
                raise
            return None
        except OSError as exc:
            raise PersistenceIOError("could not persist derived production-status alert; snapshot remains authoritative") from exc
        return {**expected, "created": True, "path": str(receipt.path), "sha256": receipt.sha256}

    def reconcile_status_alerts(self, *, limit: int = 1000) -> dict[str, Any]:
        """Backfill absent derived alerts for bounded adjacent persisted snapshots."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 2 or limit > 1000:
            raise ValueError("alert reconciliation limit must be within 2..1000 snapshots")
        rows = self.history(limit=limit)
        created: list[dict[str, Any]] = []
        material_count = 0
        for previous, current in self._chronological_transitions(rows):
            expected = derive_transition_alert(previous, previous["sha256"], current, current["sha256"])
            if expected is None:
                continue
            material_count += 1
            persisted = self._persist_transition_alert(previous, current)
            if persisted is not None:
                created.append(persisted)
        return {
            "created_alerts": created,
            "created_count": len(created),
            "material_transition_count": material_count,
        }

    def collect(self) -> dict[str, Any]:
        # Recover derived records left behind by an earlier crash before adding
        # a new point to the immutable timeline.
        recovered = self.reconcile_status_alerts()
        previous_rows = self.history(limit=1)
        previous = previous_rows[0] if previous_rows else None
        current = self.evaluate().to_dict()
        diff = compare_status_snapshots(previous, current) if previous else ProductionStatusDiff("NO_MATERIAL_CHANGE", ())
        receipt = self.store.persist(ArtifactType.PRODUCTION_STATUS, current, run_id=current["snapshot_id"])
        created_alerts = list(recovered["created_alerts"])
        if previous:
            alert = self._persist_transition_alert({**previous, "sha256": previous["sha256"]}, {**current, "sha256": receipt.sha256})
            if alert is not None:
                created_alerts.append(alert)
        if created_alerts:
            latest_alert = created_alerts[-1]
            alert_result: dict[str, Any] = {
                "created": True,
                "classification": latest_alert["classification"],
                "alert_class": latest_alert["alert_class"],
                **{key: latest_alert[key] for key in ("alert_id", "dedup_key", "path", "message") if key in latest_alert},
            }
        else:
            alert_result = {"created": False, "classification": diff.classification, "alert_class": alert_class_for(diff.classification)}
        return {
            "snapshot": current,
            "snapshot_sha256": receipt.sha256,
            "snapshot_path": str(receipt.path),
            "diff": diff.to_dict(),
            "alert": alert_result,
            "new_alerts": created_alerts,
            "reconciled_alert_count": recovered["created_count"],
        }


def validate_status_artifacts(data_root: Path = DATA_PATH) -> dict[str, Any]:
    service = ProductionStatusService(data_root=data_root)
    try:
        snapshots = service.history(limit=1000)
        alerts = service._existing_alerts()
        material = 0
        missing = 0
        for previous, current in service._chronological_transitions(snapshots):
            expected = derive_transition_alert(previous, previous["sha256"], current, current["sha256"])
            if expected is None:
                continue
            material += 1
            stored = alerts.get(expected["dedup_key"])
            if stored is None:
                missing += 1
            elif stored != expected:
                raise ArtifactCorruptError("persisted production-status alert contradicts its immutable snapshots")
    except ArtifactCorruptError:
        return {"status": "CORRUPT", "snapshot_count": 0, "alert_count": 0, "material_transition_count": 0, "missing_alert_count": 0}
    return {
        "status": "valid" if missing == 0 else "incomplete",
        "snapshot_count": len(snapshots),
        "alert_count": len(alerts),
        "material_transition_count": material,
        "missing_alert_count": missing,
    }
