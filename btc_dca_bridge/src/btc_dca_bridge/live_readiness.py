"""Phase 5.4B read-only live-execution preparation and gate.

This module deliberately has no Bybit transport and no ledger writer.  It
creates immutable preparation evidence and evaluates the checks that a future
authorization ceremony would have to satisfy.  The checked-in production
configuration therefore remains blocked at every call site.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Mapping

from .artifacts import ArtifactStore, ArtifactType
from .ttl_policy import LIVE_EXECUTION_APPROVAL_TTL
from .config import ExecutionConfig

APPROVAL_TTL = LIVE_EXECUTION_APPROVAL_TTL
APPROVAL_SCHEMA_VERSION = "5.4.0B"
MAX_CLOCK_SKEW_SECONDS = Decimal("2")
MAX_CLOCK_ROUND_TRIP_MS = Decimal("2000")
ORDER_CONTRACT = {
    "category": "spot", "symbol": "BTCUSDT", "side": "Buy",
    "orderType": "Market", "marketUnit": "quoteCoin", "isLeverage": 0,
    "orderFilter": "Order",
}


class LiveReadinessError(ValueError):
    """A fail-closed preparation or gate error."""


class GateStatus(str, Enum):
    BLOCKED = "BLOCKED"
    READY_FOR_SUBMISSION = "READY_FOR_SUBMISSION"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


def canonical_fingerprint(value: Any) -> str:
    """Return the repository's deterministic JSON SHA-256 representation."""
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LiveReadinessError("fingerprint input is not canonical JSON") from exc
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def canonical_decimal(value: Any) -> str | None:
    """Normalize an optional finite decimal for cross-artifact projections."""
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LiveReadinessError("canonical decimal is invalid") from exc
    if not parsed.is_finite():
        raise LiveReadinessError("canonical decimal is invalid")
    return format(parsed.normalize(), "f")


def instrument_execution_projection(value: Any) -> dict[str, Any]:
    """Project only execution-relevant BTCUSDT Spot instrument semantics.

    Response timestamps, ordering, descriptive fields, and unrelated symbols
    never enter this projection.  Identity fields are explicit because the
    parser proves them before constructing ``InstrumentRules``.
    """
    if isinstance(value, Mapping):
        get = value.get
    else:
        get = lambda name, default=None: getattr(value, name, default)
    return {
        "exchange": "Bybit",
        "category": "spot",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "base_coin": "BTC",
        "quote_coin": "USDT",
        "market_order_supported": True,
        "market_unit": "quoteCoin",
        "quote_minimum": canonical_decimal(get("quote_minimum")),
        "base_quantity_minimum": canonical_decimal(get("base_quantity_minimum")),
        "quantity_step": canonical_decimal(get("quantity_step")),
        "price_tick_size": canonical_decimal(get("price_tick_size")),
        "quote_precision": canonical_decimal(get("quote_precision")),
        "max_market_order_qty": canonical_decimal(get("max_market_order_qty")),
        "max_limit_order_qty": canonical_decimal(get("max_limit_order_qty")),
        "market_order_qty_unit": get("market_order_qty_unit"),
        "market_buy_quote_maximum": canonical_decimal(get("market_buy_quote_maximum")),
    }


def manifest_instrument_execution_projection(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Build the same semantic projection from a persisted CanaryManifest."""
    return instrument_execution_projection({
        "quote_minimum": manifest.get("instrument_min_order_amt"),
        "base_quantity_minimum": manifest.get("instrument_base_quantity_minimum"),
        "quantity_step": manifest.get("instrument_qty_step"),
        "price_tick_size": manifest.get("instrument_tick_size"),
        "quote_precision": manifest.get("instrument_quote_precision"),
        "max_market_order_qty": manifest.get("instrument_max_market_order_qty"),
        "max_limit_order_qty": manifest.get("instrument_max_limit_order_qty"),
        "market_order_qty_unit": manifest.get("instrument_market_order_qty_unit"),
        "market_buy_quote_maximum": manifest.get("instrument_market_buy_quote_maximum"),
    })


def gate_stability_projection(current: Mapping[str, Any]) -> dict[str, Any]:
    """Return the safety state that must not drift between Gate A and B.

    Exact balances and timestamped clock measurements are deliberately
    excluded.  Their safety is represented by threshold/freshness booleans.
    """
    safety = _safety_dict(current.get("safety_config", {}))
    keys = (
        "approval_sha256", "authorization_sha256", "repository_commit_sha",
        "calendar_month", "monthly_spent_usdt", "remaining_monthly_budget_usdt",
        "account_identity_fingerprint", "account_mode",
        "credential_scope_classification", "liability_state",
        "instrument_metadata_fingerprint", "reconciliation_evidence_fingerprint",
        "production_contract_fingerprint", "execution_safety_config_fingerprint",
        "ledger_valid", "credential_scope_accepted", "withdrawal_enabled",
        "btc_liability", "usdt_liability", "borrow_state",
        "spot_usdt_sufficient", "spot_availability_fresh",
        "instrument_metadata_fresh", "clock_evidence_fresh",
        "clock_skew_within_limit", "clock_measurement_quality_accepted",
        "reconciliation_evidence_fresh", "duplicate_execution",
        "duplicate_attempt", "reconciliation_required", "request_fingerprint",
    )
    return {"safety_config": safety, **{key: current.get(key) for key in keys}}


def account_identity_fingerprint(
    *,
    exchange: str,
    identity: str,
    unified_margin_status: Any,
    margin_mode: Any,
    spot_hedging_status: Any,
) -> str:
    """Fingerprint only stable exchange/account/key identity attributes.

    Account timestamps, balances, permissions, instruments, and runtime
    reconciliation state are deliberately outside this identity boundary.
    The pipe encoding is retained as the canonical representation for the
    already-approved production identity chain.
    """
    material = "|".join(
        (
            str(exchange),
            str(unified_margin_status),
            str(margin_mode),
            str(spot_hedging_status),
            str(identity),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise LiveReadinessError("time must be UTC-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LiveReadinessError(f"{label} is invalid") from exc
    if not result.is_finite() or result < 0:
        raise LiveReadinessError(f"{label} is invalid")
    return result


def _sha(value: Any, label: str) -> str:
    result = str(value)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise LiveReadinessError(f"{label} must be a lowercase SHA-256 digest")
    return result


def _safety_dict(config: ExecutionConfig | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(config, ExecutionConfig):
        return {
            "live_execution_enabled": config.live_execution_enabled,
            "kill_switch": config.kill_switch,
            "explicit_live_approval_required": config.explicit_live_approval_required,
            "monthly_cap_usd": str(config.monthly_cap_usd),
            "exchange": config.exchange, "market_type": config.market_type,
            "symbol": config.symbol, "order_submission": config.order_submission,
        }
    return dict(config)


def execution_safety_config_fingerprint(config: ExecutionConfig | Mapping[str, Any]) -> str:
    """Fingerprint the canonical execution-safety projection."""
    return canonical_fingerprint(_safety_dict(config))


def production_contract_fingerprint() -> str:
    """Fingerprint the exact approved order contract."""
    return canonical_fingerprint(ORDER_CONTRACT)


@dataclass(frozen=True)
class LiveExecutionApproval:
    payload: Mapping[str, Any]

    @property
    def approval_id(self) -> str:
        return str(self.payload["approval_id"])

    @property
    def expires_at_utc(self) -> str:
        return str(self.payload["expires_at_utc"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)

    @classmethod
    def prepare(
        cls, *, now_utc: datetime, repository_commit_sha: str, strategy_id: str,
        strategy_version: str, canary_manifest_id: str, canary_manifest_sha256: str,
        decision_id: str, decision_sha256: str, order_intent_id: str,
        order_intent_sha256: str, client_order_id: str, quote_amount_usdt: Any,
        calendar_month: str, monthly_spent_usdt: Any, remaining_monthly_budget_usdt: Any,
        account_identity_fingerprint: str, account_mode: str,
        credential_scope_classification: str, liability_state: str,
        spot_usdt_availability_fingerprint: str, instrument_metadata_fingerprint: str,
        clock_skew_evidence_fingerprint: str, reconciliation_evidence_fingerprint: str,
        production_contract_fingerprint: str, execution_safety_config_fingerprint: str,
        manifest_expires_at_utc: str, explicit_authorization_status: str = "NOT_AUTHORIZED",
        state: str = "BLOCKED", ttl: timedelta = APPROVAL_TTL,
    ) -> "LiveExecutionApproval":
        if ttl <= timedelta(0) or ttl > APPROVAL_TTL:
            raise LiveReadinessError("approval TTL must be positive and no more than ten minutes")
        if strategy_id != "btc_adaptive_dca_v1" or strategy_version != "1.0.0":
            raise LiveReadinessError("approval must bind the active BTC DCA V1 strategy")
        if explicit_authorization_status != "NOT_AUTHORIZED":
            raise LiveReadinessError("Phase 5.4B preparation cannot grant authorization")
        created = _timestamp(now_utc)
        expires = _timestamp(now_utc + ttl)
        amount = _decimal(quote_amount_usdt, "quote amount")
        body: dict[str, Any] = {
            "schema_version": APPROVAL_SCHEMA_VERSION, "created_at_utc": created,
            "expires_at_utc": expires, "repository_commit_sha": repository_commit_sha,
            "strategy_id": strategy_id, "strategy_version": strategy_version,
            "canary_manifest_id": canary_manifest_id, "canary_manifest_sha256": _sha(canary_manifest_sha256, "manifest SHA"),
            "decision_id": decision_id, "decision_sha256": _sha(decision_sha256, "decision SHA"),
            "order_intent_id": order_intent_id, "order_intent_sha256": _sha(order_intent_sha256, "intent SHA"),
            "client_order_id": client_order_id, "quote_amount_usdt": str(amount),
            "order_contract": dict(ORDER_CONTRACT), "calendar_month": calendar_month,
            "monthly_spent_usdt": str(_decimal(monthly_spent_usdt, "monthly spent")),
            "remaining_monthly_budget_usdt": str(_decimal(remaining_monthly_budget_usdt, "remaining budget")),
            "account_identity_fingerprint": _sha(account_identity_fingerprint, "account fingerprint"),
            "account_mode": account_mode, "credential_scope_classification": credential_scope_classification,
            "liability_state": liability_state,
            "spot_usdt_availability_fingerprint": _sha(spot_usdt_availability_fingerprint, "availability fingerprint"),
            "instrument_metadata_fingerprint": _sha(instrument_metadata_fingerprint, "instrument fingerprint"),
            "clock_skew_evidence_fingerprint": _sha(clock_skew_evidence_fingerprint, "clock fingerprint"),
            "reconciliation_evidence_fingerprint": _sha(reconciliation_evidence_fingerprint, "reconciliation fingerprint"),
            "production_contract_fingerprint": _sha(production_contract_fingerprint, "contract fingerprint"),
            "execution_safety_config_fingerprint": _sha(execution_safety_config_fingerprint, "safety fingerprint"),
            "manifest_expires_at_utc": manifest_expires_at_utc, "approval_expires_at_utc": expires,
            "state": state, "explicit_authorization_status": explicit_authorization_status,
            "no_order_executed": True,
        }
        identity = canonical_fingerprint({key: value for key, value in body.items() if key not in {"approval_id", "one_shot_identity"}})
        body["approval_id"] = "approval5b-" + identity[:32]
        body["one_shot_identity"] = f"{client_order_id}:{body['approval_id']}"
        return cls(body)

    def persist(self, store: ArtifactStore, *, run_id: str):
        return store.persist(ArtifactType.LIVE_EXECUTION_APPROVAL, self.payload, run_id=run_id)


@dataclass(frozen=True)
class GateResult:
    status: GateStatus
    gate: str
    reasons: tuple[str, ...]
    fingerprint: str

    @property
    def allowed(self) -> bool:
        return self.status is GateStatus.READY_FOR_SUBMISSION


class LiveExecutionGate:
    """Pure evaluator for Gate A/Gate B; it never constructs a network client."""

    def __init__(self, approval: LiveExecutionApproval, *, authorization: Mapping[str, Any] | None = None, approval_sha256: str | None = None, authorization_sha256: str | None = None, now: Callable[[], datetime] | None = None) -> None:
        self.approval = approval
        self.authorization = authorization
        self.approval_sha256 = approval_sha256
        self.authorization_sha256 = authorization_sha256
        self._now = now or (lambda: datetime.now(UTC))

    def evaluate(self, current: Mapping[str, Any], *, gate: str = "A") -> GateResult:
        if gate not in {"A", "B"}:
            raise LiveReadinessError("gate must be A or B")
        p = self.approval.payload
        reasons: list[str] = []
        safety = _safety_dict(current.get("safety_config", {}))
        # Kill switch is intentionally checked first and cannot be overridden.
        if safety.get("kill_switch") is True: reasons.append("kill switch is active")
        if safety.get("live_execution_enabled") is not True: reasons.append("live execution is disabled")
        if safety.get("explicit_live_approval_required") is not True: reasons.append("explicit live approval is not required")
        if safety.get("order_submission") not in {"implemented", "implemented_disabled"}: reasons.append("order submission capability is not enabled")
        if (safety.get("exchange"), safety.get("market_type"), safety.get("symbol")) != ("Bybit", "spot", "BTCUSDT"): reasons.append("production market contract mismatch")
        from .real_money_authorization import verify_authorization_binding
        authorization = current.get("real_money_authorization", self.authorization)
        valid_authorization, authorization_reasons = verify_authorization_binding(
            self.approval, authorization,
            approval_sha256=current.get("approval_sha256", self.approval_sha256),
            authorization_sha256=current.get("authorization_sha256", self.authorization_sha256),
            now_utc=self._now(),
        )
        if not valid_authorization: reasons.extend(authorization_reasons)
        try:
            now = self._now().astimezone(UTC)
            expires = datetime.fromisoformat(str(p["expires_at_utc"]).replace("Z", "+00:00"))
            if now >= expires: reasons.append("approval is expired")
        except (KeyError, ValueError): reasons.append("approval expiry is malformed")
        if p.get("state") != "PREPARED": reasons.append("approval is not PREPARED")
        if current.get("approval_consumed") or current.get("submission_attempted") or current.get("submission_outcome"): reasons.append("approval has prior use or outcome")
        if current.get("repository_commit_sha") != p.get("repository_commit_sha"): reasons.append("repository commit changed")
        # Immutable identity/configuration remains exact.  Balance snapshots,
        # clock measurements, and ledger totals are mutable conditions and are
        # checked below using fresh semantic thresholds instead.
        for key in ("canary_manifest_id", "canary_manifest_sha256", "decision_id", "decision_sha256", "order_intent_id", "order_intent_sha256", "client_order_id", "quote_amount_usdt", "calendar_month", "account_identity_fingerprint", "account_mode", "credential_scope_classification", "liability_state", "instrument_metadata_fingerprint", "reconciliation_evidence_fingerprint", "production_contract_fingerprint", "execution_safety_config_fingerprint"):
            if key in current and str(current[key]) != str(p.get(key)): reasons.append(f"{key} changed")
        if current.get("calendar_month") != p.get("calendar_month"): reasons.append("UTC month changed")
        if _decimal(current.get("remaining_monthly_budget_usdt", "0"), "current remaining budget") < _decimal(p["quote_amount_usdt"], "approved amount"): reasons.append("monthly budget is insufficient")
        if current.get("ledger_valid") is not True: reasons.append("canonical ledger is not valid")
        if current.get("duplicate_execution") or current.get("duplicate_attempt") or current.get("reconciliation_required"): reasons.append("duplicate or unresolved reconciliation state")
        if current.get("credential_scope_accepted") is not True: reasons.append("credential scope is not accepted")
        if current.get("withdrawal_enabled") is True: reasons.append("credential permits withdrawal")
        if current.get("btc_liability") or current.get("usdt_liability") or current.get("borrow_state"): reasons.append("account liability or borrow state is unsafe")
        if current.get("spot_usdt_sufficient") is not True: reasons.append("authoritative Spot USDT availability is insufficient")
        if current.get("spot_availability_fresh") is not True: reasons.append("Spot availability evidence is stale")
        if current.get("instrument_metadata_fresh") is not True: reasons.append("instrument metadata is stale")
        if current.get("clock_evidence_fresh") is not True: reasons.append("clock evidence is stale")
        if current.get("clock_skew_within_limit") is not True: reasons.append("clock skew exceeds the approved threshold")
        if current.get("clock_measurement_quality_accepted") is not True: reasons.append("clock measurement quality is unacceptable")
        if current.get("reconciliation_evidence_fresh") is not True: reasons.append("reconciliation evidence is stale")
        if current.get("account_identity_fingerprint") != p.get("account_identity_fingerprint"): reasons.append("account identity changed")
        fingerprint = canonical_fingerprint({"approval": p, "current": gate_stability_projection(current)})
        if reasons: return GateResult(GateStatus.BLOCKED, gate, tuple(dict.fromkeys(reasons)), fingerprint)
        return GateResult(GateStatus.READY_FOR_SUBMISSION, gate, (), fingerprint)

    def evaluate_a_then_b(self, gate_a: Mapping[str, Any], gate_b: Mapping[str, Any]) -> tuple[GateResult, GateResult]:
        first = self.evaluate(gate_a, gate="A")
        second = self.evaluate(gate_b, gate="B")
        if first.allowed and first.fingerprint != second.fingerprint:
            second = GateResult(GateStatus.BLOCKED, "B", tuple(dict.fromkeys((*second.reasons, "current evidence changed between Gate A and Gate B"))), second.fingerprint)
        return first, second


def prepare_submission_attempt(*, store: ArtifactStore, run_id: str, approval: LiveExecutionApproval, approval_sha256: str, gate_result: GateResult, repository_commit_sha: str, decision_id: str, decision_sha256: str, order_intent_id: str, order_intent_sha256: str, quote_amount_usdt: Any, account_identity_fingerprint: str, calendar_month: str, reconciliation_fingerprint: str):
    if not gate_result.allowed:
        raise LiveReadinessError("submission attempt cannot be prepared before all gates pass")
    try:
        persisted, persisted_sha = store.find_artifact(ArtifactType.LIVE_EXECUTION_APPROVAL, identity_field="approval_id", identity_value=approval.approval_id)
    except Exception as exc:
        raise LiveReadinessError("persisted Phase 5.4B approval is required") from exc
    if persisted != approval.to_dict() or persisted_sha != approval_sha256:
        raise LiveReadinessError("approval bytes or SHA-256 sidecar do not match")
    amount = _decimal(quote_amount_usdt, "quote amount")
    request = {**ORDER_CONTRACT, "qty": str(amount), "orderLinkId": approval.payload["client_order_id"]}
    request_fp = canonical_fingerprint(request)
    # Keep the attempt in the same canonical UTC date as its persisted
    # approval.  Using wall-clock ``now`` here can place the immutable
    # attempt under a different date than callers use to reread the chain.
    created_at_utc = str(approval.payload["created_at_utc"])
    seed = {"approval_id": approval.approval_id, "approval_sha256": approval_sha256, "client_order_id": approval.payload["client_order_id"], "request_fingerprint": request_fp, "created_at_utc": created_at_utc}
    payload = {"schema_version": APPROVAL_SCHEMA_VERSION, "attempt_id": "attempt5b-" + canonical_fingerprint(seed)[:32], "created_at_utc": seed["created_at_utc"], "approval_id": approval.approval_id, "approval_sha256": _sha(approval_sha256, "approval SHA"), "canary_manifest_id": approval.payload["canary_manifest_id"], "canary_manifest_sha256": approval.payload["canary_manifest_sha256"], "decision_id": decision_id, "decision_sha256": _sha(decision_sha256, "decision SHA"), "order_intent_id": order_intent_id, "order_intent_sha256": _sha(order_intent_sha256, "intent SHA"), "client_order_id": approval.payload["client_order_id"], "quote_amount_usdt": str(amount), "safe_request_fingerprint": request_fp, "repository_commit_sha": repository_commit_sha, "account_identity_fingerprint": _sha(account_identity_fingerprint, "account fingerprint"), "calendar_month": calendar_month, "safety_gate_fingerprint": gate_result.fingerprint, "reconciliation_fingerprint": _sha(reconciliation_fingerprint, "reconciliation fingerprint"), "status": "PREPARED_FOR_SUBMISSION", "no_order_executed": True}
    return store.persist(ArtifactType.LIVE_EXECUTION_ATTEMPT, payload, run_id=run_id)


def submit_disabled(*_args: Any, **_kwargs: Any) -> None:
    """The Phase 5.4B boundary has no transport by design."""
    raise LiveReadinessError("REAL MONEY EXECUTION NOT AUTHORIZED")


def classify_submission_outcome(outcome: Mapping[str, Any]) -> str:
    """Only canonical confirmed reconciliation can permit a later ledger step."""
    if outcome.get("reconciliation_state") != "confirmed":
        return GateStatus.RECONCILIATION_REQUIRED.value
    if outcome.get("client_order_id") is None or not outcome.get("fills"):
        return GateStatus.RECONCILIATION_REQUIRED.value
    return "CONFIRMED_EXECUTION"


def ledger_append_allowed(outcome: Mapping[str, Any]) -> bool:
    return classify_submission_outcome(outcome) == "CONFIRMED_EXECUTION" and outcome.get("ledger_mutated") is not True

