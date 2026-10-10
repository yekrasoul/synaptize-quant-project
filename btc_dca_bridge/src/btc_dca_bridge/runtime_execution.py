"""Ephemeral runtime policy derived from the immutable live-execution ceremony.

The checked-in execution policy is intentionally permanently fail-closed.  This
module creates a process-local enabled view only after the persisted ceremony
artifacts have been verified.  It is never a standing authorization and is
bound to one order identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Mapping

from .config import ExecutionConfig
from .live_readiness import LiveReadinessError, ORDER_CONTRACT, canonical_fingerprint
from .real_money_authorization import artifact_sha256

MAX_AUTHORIZATION_SECONDS = 10 * 60
MAX_ACTIVATION_SECONDS = 60
MAX_ENABLEMENT_SECONDS = 24 * 60 * 60


def _when(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LiveReadinessError(f"{label} expiry is malformed") from exc
    if parsed.tzinfo is None:
        raise LiveReadinessError(f"{label} expiry must be UTC")
    return parsed.astimezone(UTC)


def _within_ttl(payload: Mapping[str, Any], label: str, maximum: int) -> None:
    created = _when(payload.get("created_at_utc"), label)
    expires = _when(payload.get("expires_at_utc"), label)
    if expires <= created or (expires - created).total_seconds() > maximum:
        raise LiveReadinessError(f"{label} TTL exceeds its governed maximum")


def _same(values: list[tuple[str, Any]]) -> None:
    first_name, first = values[0]
    for name, value in values[1:]:
        if str(value) != str(first):
            raise LiveReadinessError(f"{name} does not match {first_name}")


def _intent_dict(intent: Any) -> Mapping[str, Any]:
    return intent.to_dict() if hasattr(intent, "to_dict") else intent


def _canonical_config(config: ExecutionConfig) -> None:
    if config.live_execution_enabled is not False:
        raise LiveReadinessError("canonical execution config must remain disabled")
    if config.kill_switch is not True:
        raise LiveReadinessError("canonical execution config must keep the kill switch active")
    if config.order_submission != "not_implemented":
        raise LiveReadinessError("canonical execution config must keep submission not_implemented")
    if config.monthly_cap_usd != Decimal("500"):
        raise LiveReadinessError("canonical monthly cap must remain 500 USD")
    if (config.exchange, config.market_type, config.symbol) != ("Bybit", "spot", "BTCUSDT"):
        raise LiveReadinessError("canonical market contract is invalid")


def _enabled_runtime(config: ExecutionConfig) -> ExecutionConfig:
    return ExecutionConfig(
        schema_version=config.schema_version,
        live_execution_enabled=True,
        kill_switch=False,
        explicit_live_approval_required=True,
        monthly_cap_usd=Decimal("500"),
        exchange="Bybit", market_type="spot", symbol="BTCUSDT",
        order_submission="implemented",
    )


@dataclass(frozen=True)
class EphemeralExecutionPolicy:
    """One-process enabled policy bound to one exact canary identity."""

    runtime_config: ExecutionConfig
    one_shot_identity: str
    client_order_id: str
    quote_amount_usdt: str
    calendar_month: str
    expires_at_utc: str
    request_fingerprint: str

    def assert_current(self, *, client_order_id: str, quote_amount_usdt: Any, calendar_month: str, now_utc: datetime) -> None:
        if now_utc.astimezone(UTC) >= _when(self.expires_at_utc, "ephemeral policy"):
            raise LiveReadinessError("ephemeral execution policy is expired")
        if (client_order_id, str(quote_amount_usdt), calendar_month) != (self.client_order_id, self.quote_amount_usdt, self.calendar_month):
            raise LiveReadinessError("ephemeral execution identity changed")


def derive_ephemeral_execution_policy(
    *, canonical_config: ExecutionConfig, enablement: Mapping[str, Any], enablement_sha256: str,
    authorization: Mapping[str, Any], authorization_sha256: str,
    activation: Mapping[str, Any], activation_sha256: str,
    approval: Mapping[str, Any], approval_sha256: str,
    manifest: Mapping[str, Any], manifest_sha256: str,
    decision: Mapping[str, Any], decision_sha256: str,
    intent: Any, intent_sha256: str, evidence: Mapping[str, Any],
    repository_commit_sha: str, calendar_month: str, now_utc: datetime,
) -> EphemeralExecutionPolicy:
    """Derive an enabled runtime only from an already verified ceremony."""
    _canonical_config(canonical_config)
    for label, payload, digest in (
        ("ProductionEnablement", enablement, enablement_sha256),
        ("RealMoneyAuthorization", authorization, authorization_sha256),
        ("ExecutionActivation", activation, activation_sha256),
        ("LiveExecutionApproval", approval, approval_sha256),
        ("CanaryManifest", manifest, manifest_sha256),
        ("Decision", decision, decision_sha256),
        ("OrderIntent", _intent_dict(intent), intent_sha256),
    ):
        if artifact_sha256(payload) != digest:
            raise LiveReadinessError(f"{label} SHA-256 binding is invalid")

    _within_ttl(enablement, "ProductionEnablement", MAX_ENABLEMENT_SECONDS)
    _within_ttl(authorization, "RealMoneyAuthorization", MAX_AUTHORIZATION_SECONDS)
    _within_ttl(activation, "ExecutionActivation", MAX_ACTIVATION_SECONDS)
    for label, payload in (("ProductionEnablement", enablement), ("RealMoneyAuthorization", authorization), ("ExecutionActivation", activation), ("LiveExecutionApproval", approval)):
        if now_utc.astimezone(UTC) >= _when(payload.get("expires_at_utc"), label):
            raise LiveReadinessError(f"{label} is expired")

    if authorization.get("status") != "AUTHORIZED" or authorization.get("consumed") is not False:
        raise LiveReadinessError("RealMoneyAuthorization is not usable")
    if activation.get("status") != "ACTIVE" or activation.get("consumed") is not False:
        raise LiveReadinessError("ExecutionActivation is not usable")
    if approval.get("state") != "PREPARED" or approval.get("explicit_authorization_status") != "NOT_AUTHORIZED":
        raise LiveReadinessError("LiveExecutionApproval is not a preparation artifact")
    if enablement.get("status") not in {"PREPARED", "ACTIVE"}:
        raise LiveReadinessError("ProductionEnablement is not usable")

    intent_payload = _intent_dict(intent)
    _same([
        ("approval_id", authorization.get("approval_id")),
        ("approval.approval_id", approval.get("approval_id")),
    ])
    _same([
        ("canary_id", approval.get("canary_manifest_id")),
        ("manifest.canary_id", manifest.get("canary_id")),
    ])
    if any(payload.get("strategy_id") != "btc_adaptive_dca_v1" for payload in (approval, manifest, enablement, intent_payload)):
        raise LiveReadinessError("ceremony strategy identity is not BTC DCA V1")
    _same([
        ("decision_id", approval.get("decision_id")),
        ("decision.decision_id", decision.get("decision_id")),
        ("intent.decision_id", intent_payload.get("decision_id")),
    ])
    _same([
        ("order_intent_id", approval.get("order_intent_id")),
        ("intent.order_intent_id", intent_payload.get("order_intent_id")),
    ])
    _same([
        ("client_order_id", approval.get("client_order_id")),
        ("authorization.client_order_id", authorization.get("client_order_id")),
        ("activation.client_order_id", activation.get("client_order_id")),
        ("intent.client_order_id", intent_payload.get("client_order_id")),
    ])
    amounts = [
        approval.get("quote_amount_usdt"), authorization.get("exact_quote_amount_usdt"),
        activation.get("exact_quote_amount_usdt"), intent_payload.get("quote_amount_usd"),
    ]
    try:
        if len({Decimal(str(amount)) for amount in amounts}) != 1:
            raise LiveReadinessError("quote amount does not match the immutable ceremony")
    except Exception as exc:
        if isinstance(exc, LiveReadinessError):
            raise
        raise LiveReadinessError("quote amount is malformed") from exc
    _same([
        ("calendar_month", approval.get("calendar_month")),
        ("authorization.calendar_month", authorization.get("calendar_month")),
        ("activation.calendar_month", activation.get("calendar_month")),
        ("intent.calendar_month", intent_payload.get("calendar_month")),
        ("requested.calendar_month", calendar_month),
    ])
    _same([
        ("repository_commit_sha", approval.get("repository_commit_sha")),
        ("authorization.repository_commit_sha", authorization.get("repository_commit_sha")),
        ("activation.repository_commit_sha", activation.get("repository_commit_sha")),
        ("enablement.repository_commit_sha", enablement.get("repository_commit_sha")),
        ("requested.repository_commit_sha", repository_commit_sha),
    ])

    _same([
        ("manifest_sha256", approval.get("canary_manifest_sha256")),
        ("activation.manifest_sha256", activation.get("canary_manifest_sha256")),
        ("authorization.manifest_sha256", authorization.get("canary_manifest_sha256")),
        ("persisted.manifest_sha256", manifest_sha256),
    ])
    _same([
        ("approval_sha256", authorization.get("approval_sha256")),
        ("activation.approval_sha256", activation.get("approval_sha256")),
        ("persisted.approval_sha256", approval_sha256),
    ])
    _same([
        ("decision_sha256", approval.get("decision_sha256")),
        ("activation.decision_sha256", activation.get("decision_sha256")),
        ("persisted.decision_sha256", decision_sha256),
    ])
    _same([
        ("order_intent_sha256", approval.get("order_intent_sha256")),
        ("activation.order_intent_sha256", activation.get("order_intent_sha256")),
        ("persisted.order_intent_sha256", intent_sha256),
    ])
    _same([
        ("account_identity_fingerprint", approval.get("account_identity_fingerprint")),
        ("authorization.account_identity_fingerprint", authorization.get("account_identity_fingerprint")),
        ("activation.account_identity_fingerprint", activation.get("account_identity_fingerprint")),
        ("enablement.expected_account_identity_fingerprint", enablement.get("expected_account_identity_fingerprint")),
    ])
    _same([
        ("execution_policy_fingerprint", approval.get("execution_safety_config_fingerprint")),
        ("activation.execution_safety_config_fingerprint", activation.get("execution_safety_config_fingerprint")),
        ("enablement.execution_policy_fingerprint", enablement.get("execution_policy_fingerprint")),
    ])
    _same([
        ("production_contract_fingerprint", approval.get("production_contract_fingerprint")),
        ("activation.production_contract_fingerprint", activation.get("production_contract_fingerprint")),
        ("enablement.production_contract_fingerprint", enablement.get("production_contract_fingerprint")),
    ])
    if approval.get("order_contract") != ORDER_CONTRACT:
        raise LiveReadinessError("approved order contract is not the exact Bybit Spot BTCUSDT contract")
    if evidence.get("real_money_authorization", {}).get("status") != "NOT_AUTHORIZED":
        raise LiveReadinessError("persisted evidence crosses the authorization boundary")
    unresolved = evidence.get("unresolved_operations_result")
    if not isinstance(unresolved, Mapping) or unresolved.get("status") not in {"PASS", "HEALTHY", "CLEAR"}:
        raise LiveReadinessError("persisted evidence reports unresolved submission or reconciliation state")

    request_fingerprint = canonical_fingerprint({**ORDER_CONTRACT, "qty": str(approval["quote_amount_usdt"]), "orderLinkId": approval["client_order_id"]})
    return EphemeralExecutionPolicy(
        runtime_config=_enabled_runtime(canonical_config),
        one_shot_identity=str(approval["one_shot_identity"]),
        client_order_id=str(approval["client_order_id"]),
        quote_amount_usdt=str(approval["quote_amount_usdt"]),
        calendar_month=str(approval["calendar_month"]),
        expires_at_utc=str(activation["expires_at_utc"]),
        request_fingerprint=request_fingerprint,
    )
