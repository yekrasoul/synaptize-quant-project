"""Phase 5.4E production governance boundary.

ProductionEnablement is deployment policy evidence, never an order approval or
an execution switch.  The checked-in execution policy remains fail closed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from .artifacts import ArtifactStore, ArtifactType
from .config import ExecutionConfig
from .errors import ArtifactNotFoundError
from .live_readiness import GateStatus, LiveReadinessError, canonical_fingerprint
from .real_money_authorization import artifact_sha256

ENABLEMENT_TTL = timedelta(hours=24)
ENABLEMENT_SCHEMA_VERSION = "5.4.0E"
ENABLEMENT_CONFIRMATION = "ENABLE CONTROLLED BYBIT SPOT BTCUSDT ONE-SHOT EXECUTION POLICY"
_SHA = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_EVIDENCE = (
    "canonical_ci_healthy", "ledger_valid", "v1_strategy_valid", "production_contract_valid",
    "account_identity_known", "account_mode_accepted", "liabilities_clear", "credential_scope_accepted",
    "withdrawal_disabled", "spot_availability_operational", "instrument_metadata_operational",
    "clock_operational", "reconciliation_operational", "production_shadow_healthy", "watchdog_healthy",
    "no_unresolved_submission", "no_reconciliation_required", "no_artifact_corruption",
)


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise LiveReadinessError("production enablement time must be UTC-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _expiry(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveReadinessError("production enablement expiry is malformed") from exc
    if parsed.tzinfo is None:
        raise LiveReadinessError("production enablement expiry must be UTC")
    return parsed.astimezone(UTC)


def confirmation_digest(confirmation: str) -> str:
    if confirmation != ENABLEMENT_CONFIRMATION:
        raise LiveReadinessError("exact production enablement ceremony confirmation is required")
    return hashlib.sha256(confirmation.encode("utf-8")).hexdigest()


def _sha(value: Any, label: str) -> str:
    result = str(value)
    if not _SHA.fullmatch(result):
        raise LiveReadinessError(f"{label} must be a lowercase SHA-256 digest")
    return result


@dataclass(frozen=True)
class ProductionEnablement:
    payload: Mapping[str, Any]

    @property
    def enablement_id(self) -> str:
        return str(self.payload["enablement_id"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)

    def persist(self, store: ArtifactStore, *, run_id: str):
        return store.persist(ArtifactType.PRODUCTION_ENABLEMENT, self.payload, run_id=run_id)

    @classmethod
    def prepare(cls, *, evidence: Mapping[str, Any], repository_commit_sha: str,
                execution_policy_fingerprint: str, production_contract_fingerprint: str,
                account_identity_fingerprint: str, account_mode: str,
                credential_scope: str, confirmation: str, now_utc: datetime) -> "ProductionEnablement":
        if any(evidence.get(key) is not True for key in REQUIRED_EVIDENCE):
            missing = [key for key in REQUIRED_EVIDENCE if evidence.get(key) is not True]
            raise LiveReadinessError("production enablement prerequisites are not proven: " + ", ".join(missing))
        if evidence.get("monthly_cap_usd", 500) != 500:
            raise LiveReadinessError("production enablement monthly cap must remain 500 USD")
        if not repository_commit_sha or not account_mode or not credential_scope:
            raise LiveReadinessError("identity and runtime policy evidence are required")
        digest = confirmation_digest(confirmation)
        now = now_utc.astimezone(UTC)
        body: dict[str, Any] = {
            "schema_version": ENABLEMENT_SCHEMA_VERSION, "created_at_utc": _stamp(now),
            "expires_at_utc": _stamp(now + ENABLEMENT_TTL), "status": "PREPARED",
            "repository_commit_sha": repository_commit_sha, "strategy_id": "btc_adaptive_dca_v1", "strategy_version": "1.0.0",
            "exchange": "Bybit", "market_type": "spot", "symbol": "BTCUSDT", "allowed_side": "Buy",
            "allowed_order_type": "Market", "allowed_market_unit": "quoteCoin", "leverage_allowed": False,
            "monthly_cap_usd": 500, "execution_policy_fingerprint": _sha(execution_policy_fingerprint, "execution policy fingerprint"),
            "production_contract_fingerprint": _sha(production_contract_fingerprint, "production contract fingerprint"),
            "expected_account_identity_fingerprint": _sha(account_identity_fingerprint, "account identity fingerprint"),
            "expected_account_mode": account_mode, "allowed_credential_scope": credential_scope,
            "kill_switch_policy": "REQUIRED", "explicit_authorization_required": True, "explicit_activation_required": True,
            "one_shot_only": True, "auto_retry_allowed": False, "standing_authorization_allowed": False,
            "standing_activation_allowed": False, "created_by_operator_ceremony": True,
            "operator_confirmation_digest": digest, "no_order_executed": True,
        }
        body["enablement_id"] = "enablement-" + canonical_fingerprint(body)[:32]
        return cls(body)


def prepare_production_enablement(*, store: ArtifactStore, evidence: Mapping[str, Any], repository_commit_sha: str,
                                  execution_policy_fingerprint: str, production_contract_fingerprint: str,
                                  account_identity_fingerprint: str, account_mode: str, credential_scope: str,
                                  confirmation: str, run_id: str, now_utc: datetime):
    enablement = ProductionEnablement.prepare(evidence=evidence, repository_commit_sha=repository_commit_sha,
        execution_policy_fingerprint=execution_policy_fingerprint, production_contract_fingerprint=production_contract_fingerprint,
        account_identity_fingerprint=account_identity_fingerprint, account_mode=account_mode,
        credential_scope=credential_scope, confirmation=confirmation, now_utc=now_utc)
    try:
        store.find_artifact(ArtifactType.PRODUCTION_ENABLEMENT, identity_field="enablement_id", identity_value=enablement.enablement_id)
    except ArtifactNotFoundError:
        pass
    else:
        raise LiveReadinessError("identical production enablement already exists")
    return enablement, enablement.persist(store, run_id=run_id)


def _revoked(store: ArtifactStore, enablement_id: str) -> bool:
    try:
        store.find_artifact(ArtifactType.PRODUCTION_ENABLEMENT_REVOCATION, identity_field="enablement_id", identity_value=enablement_id)
        return True
    except ArtifactNotFoundError:
        return False


@dataclass(frozen=True)
class GateDResult:
    status: GateStatus
    reasons: tuple[str, ...]
    fingerprint: str


def gate_d(*, enablement: Mapping[str, Any] | None, enablement_sha256: str | None,
           runtime_config: ExecutionConfig | Mapping[str, Any], store: ArtifactStore,
           repository_commit_sha: str | None = None, evidence: Mapping[str, Any] | None = None,
           now_utc: datetime) -> GateDResult:
    reasons: list[str] = []
    if isinstance(runtime_config, ExecutionConfig):
        config = runtime_config.__dict__
    else:
        config = dict(runtime_config)
    config = {key: (str(value) if key == "monthly_cap_usd" else value) for key, value in config.items()}
    # Absolute priority: no governance artifact or runtime policy can override this.
    if config.get("kill_switch") is True:
        reasons.append("kill switch is active")
    if enablement is None:
        reasons.append("ProductionEnablement is absent")
    else:
        try:
            from .schemas import validate_artifact
            validate_artifact("production_enablement", dict(enablement))
        except Exception as exc:
            reasons.append(f"ProductionEnablement is invalid: {exc}")
        if enablement_sha256 is None or artifact_sha256(enablement) != enablement_sha256:
            reasons.append("ProductionEnablement SHA-256 is invalid")
        if _revoked(store, str(enablement.get("enablement_id", ""))):
            reasons.append("ProductionEnablement is revoked")
        try:
            if now_utc.astimezone(UTC) >= _expiry(enablement.get("expires_at_utc")):
                reasons.append("ProductionEnablement is expired")
            created = _expiry(enablement.get("created_at_utc"))
            if _expiry(enablement.get("expires_at_utc")) - created > ENABLEMENT_TTL:
                reasons.append("ProductionEnablement TTL exceeds 24 hours")
        except LiveReadinessError as exc:
            reasons.append(str(exc))
        if enablement.get("status") not in {"PREPARED", "ACTIVE"}:
            reasons.append("ProductionEnablement is not usable")
        if repository_commit_sha is not None and enablement.get("repository_commit_sha") != repository_commit_sha:
            reasons.append("repository commit does not match ProductionEnablement")
        if enablement.get("monthly_cap_usd") != 500 or enablement.get("leverage_allowed") is not False:
            reasons.append("production policy broadens the approved cap or leverage")
        if evidence is not None:
            if evidence.get("execution_policy_fingerprint") is not None and evidence.get("execution_policy_fingerprint") != enablement.get("execution_policy_fingerprint"):
                reasons.append("execution policy fingerprint does not match ProductionEnablement")
            for key, expected in (("account_identity_fingerprint", "expected_account_identity_fingerprint"), ("account_mode", "expected_account_mode"), ("production_contract_fingerprint", "production_contract_fingerprint")):
                if evidence.get(key) != enablement.get(expected): reasons.append(f"{key} does not match ProductionEnablement")
            if evidence.get("credential_scope") != enablement.get("allowed_credential_scope"): reasons.append("credential scope does not match ProductionEnablement")
            if any(evidence.get(key) is not True for key in REQUIRED_EVIDENCE): reasons.append("required runtime evidence is incomplete")
    if config.get("live_execution_enabled") is not True: reasons.append("live execution is disabled")
    if config.get("order_submission") != "implemented": reasons.append("order submission is not enabled")
    fingerprint = canonical_fingerprint({"enablement": enablement, "config": config, "evidence": evidence})
    return GateDResult(GateStatus.BLOCKED if reasons else GateStatus.READY_FOR_SUBMISSION, tuple(dict.fromkeys(reasons)), fingerprint)


def read_enablement_status(*, store: ArtifactStore, enablement_id: str | None = None,
                           now_utc: datetime | None = None, runtime_config: ExecutionConfig | None = None) -> dict[str, Any]:
    base = {"enablement": "NOT_ENABLED", "authorization": "NOT_AUTHORIZED", "activation": "NOT_ACTIVATED", "execution": "BLOCKED", "live_execution_enabled": False, "kill_switch": True, "order_submission": "not_implemented", "message": "NO ORDER EXECUTED"}
    try:
        payload, digest = store.find_artifact(ArtifactType.PRODUCTION_ENABLEMENT, identity_field="enablement_id", identity_value=enablement_id) if enablement_id else _latest(store)
    except ArtifactNotFoundError:
        return base
    except Exception:
        base["enablement"] = "INVALID"
        return base
    now = now_utc or datetime.now(UTC)
    try:
        status = "REVOKED" if _revoked(store, str(payload["enablement_id"])) else ("EXPIRED" if now >= _expiry(payload["expires_at_utc"]) else payload.get("status", "INVALID"))
        from .schemas import validate_artifact
        validate_artifact("production_enablement", payload)
        if artifact_sha256(payload) != digest: status = "INVALID"
    except Exception:
        status = "INVALID"
    config = runtime_config
    if config is not None:
        base.update(live_execution_enabled=config.live_execution_enabled, kill_switch=config.kill_switch, order_submission=config.order_submission)
    base["enablement"] = status
    return base


def _latest(store: ArtifactStore) -> tuple[dict[str, Any], str]:
    root = store.root / "production_enablements"
    paths = sorted(root.glob("*/*/*/*.json"))
    if not paths: raise ArtifactNotFoundError("ProductionEnablement not found")
    path = paths[-1]; parts = path.parts
    date = datetime(int(parts[-4]), int(parts[-3]), int(parts[-2]), tzinfo=UTC)
    return store.read(ArtifactType.PRODUCTION_ENABLEMENT, run_id=path.stem, artifact_date_utc=date), artifact_sha256(store.read(ArtifactType.PRODUCTION_ENABLEMENT, run_id=path.stem, artifact_date_utc=date))


class ControlledProductionExecutor:
    """The sole internal composition point for a future enabled invocation.

    It accepts already-created, verified preparation artifacts.  It never
    creates authorization or activation and uses a lazy transport factory so
    blocked paths cannot even construct a submission client.
    """

    def __init__(self, *, engine: Any, store: ArtifactStore):
        self.engine = engine
        self.store = store

    def execute(self, *, enablement: Mapping[str, Any], enablement_sha256: str,
                authorization: Mapping[str, Any], authorization_sha256: str,
                activation: Mapping[str, Any], activation_sha256: str,
                capability: Any, approval: Any, approval_sha256: str,
                manifest: Mapping[str, Any], manifest_sha256: str, intent: Any,
                decision: Mapping[str, Any], runtime_config: ExecutionConfig,
                evidence: Mapping[str, Any], repository_commit_sha: str,
                calendar_month: str, ledger_path: Any, read_client: Any,
                reconciler: Any, run_id: str, transport_factory: Any):
        result = gate_d(enablement=enablement, enablement_sha256=enablement_sha256,
                        runtime_config=runtime_config, store=self.store,
                        repository_commit_sha=repository_commit_sha,
                        evidence=evidence, now_utc=datetime.now(UTC))
        if result.status is not GateStatus.READY_FOR_SUBMISSION:
            return {"status": "blocked", "gate": "D", "reasons": result.reasons, "message": "NO ORDER EXECUTED"}
        if authorization.get("status") != "AUTHORIZED" or activation.get("status") != "ACTIVE":
            return {"status": "blocked", "gate": "authorization_or_activation", "message": "NO ORDER EXECUTED"}
        # LiveOrderEngine performs the fresh Gate A/B checks and persists the
        # attempt before Gate C.  The factory is deliberately passed through
        # untouched and is called only after that engine reaches Gate C.
        submission = self.engine.submit(
            intent, decision, calendar_month=calendar_month, ledger_path=ledger_path,
            execution_config=runtime_config, approval=approval,
            approval_sha256=approval_sha256, manifest=manifest,
            manifest_sha256=manifest_sha256, read_client=read_client,
            transport_factory=transport_factory, run_id=run_id,
            post_ack_reconciler=reconciler, activation=activation,
            activation_sha256=activation_sha256, capability=capability,
        )
        return submission
