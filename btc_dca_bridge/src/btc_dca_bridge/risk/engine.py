"""Authoritative platform capital and safety policy.

Venue adapters validate native quantity/precision rules; this module owns
capital, venue/market policy, identity, freshness, duplicate, and kill-switch
decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..core.models import AccountIdentity, OrderIntent, Side


@dataclass(frozen=True)
class MarketEvidenceBinding:
    decision_id: str
    market_snapshot_id: str
    market_snapshot_sha256: str
    sentiment_snapshot_id: str
    sentiment_snapshot_sha256: str
    verified: bool
    fear_greed_index: int | None = None


@dataclass(frozen=True)
class RiskContext:
    calendar_month: str
    monthly_spend: Decimal
    monthly_cap: Decimal
    runtime_execution_enabled: bool
    kill_switch_active: bool
    market_data_fresh: bool
    account_identity: AccountIdentity | None
    expected_account_fingerprint: str
    quote_balance: Decimal
    unresolved_execution_exists: bool
    duplicate_execution_exists: bool
    venue_health: str = "healthy"
    allowed_venue: str = "bybit"
    allowed_market: str = "BTC/USDT:spot"
    leverage_enabled: bool = False
    borrowing_enabled: bool = False
    market_evidence: MarketEvidenceBinding | None = None


@dataclass(frozen=True)
class RiskAssessment:
    status: str
    reason_codes: tuple[str, ...]
    remaining_monthly_allocation: Decimal

    @property
    def approved(self) -> bool:
        return self.status == "APPROVED"


class RiskEngine:
    """One compact, deterministic safety evaluator for an OrderIntent."""

    def evaluate(self, intent: OrderIntent, context: RiskContext) -> RiskAssessment:
        reasons: list[str] = []
        if context.kill_switch_active:
            reasons.append("KILL_SWITCH_ACTIVE")
        if not context.runtime_execution_enabled:
            reasons.append("EXECUTION_POLICY_DISABLED")
        if context.venue_health != "healthy":
            reasons.append("VENUE_HEALTH_NOT_HEALTHY")
        if intent.preferred_venue != context.allowed_venue:
            reasons.append("VENUE_NOT_ALLOWED")
        if f"{intent.market.canonical_symbol}:{intent.market.instrument_type}" != context.allowed_market:
            reasons.append("MARKET_NOT_ALLOWED")
        if intent.market.instrument_type != "spot":
            reasons.append("MARKET_NOT_SPOT")
        if intent.side is not Side.BUY:
            reasons.append("SIDE_NOT_ALLOWED")
        if context.leverage_enabled:
            reasons.append("LEVERAGE_PROHIBITED")
        if context.borrowing_enabled:
            reasons.append("BORROWING_PROHIBITED")
        if not context.market_data_fresh:
            reasons.append("STALE_MARKET_DATA")
        evidence = context.market_evidence
        if (
            evidence is None
            or not evidence.verified
            or evidence.decision_id != intent.decision_id
            or evidence.market_snapshot_id != intent.market_snapshot_id
            or evidence.market_snapshot_sha256 != intent.market_snapshot_sha256
            or evidence.sentiment_snapshot_id != intent.sentiment_snapshot_id
            or evidence.sentiment_snapshot_sha256 != intent.sentiment_snapshot_sha256
            or evidence.fear_greed_index != intent.fear_greed_index
        ):
            reasons.append("MARKET_DATA_EVIDENCE_UNVERIFIED")
        if context.account_identity is None or context.account_identity.fingerprint != context.expected_account_fingerprint:
            reasons.append("ACCOUNT_IDENTITY_MISMATCH")
        if context.unresolved_execution_exists:
            reasons.append("UNRESOLVED_EXECUTION")
        if context.duplicate_execution_exists:
            reasons.append("DUPLICATE_EXECUTION_IDENTITY")
        if intent.quote_quantity > context.quote_balance:
            reasons.append("INSUFFICIENT_ALLOCATED_CAPITAL")
        remaining = context.monthly_cap - context.monthly_spend
        if context.monthly_spend < 0 or context.monthly_cap != Decimal("500"):
            reasons.append("INVALID_MONTHLY_CAP_POLICY")
        if intent.quote_quantity > remaining:
            reasons.append("MONTHLY_CAP_EXCEEDED")
        return RiskAssessment("BLOCKED" if reasons else "APPROVED", tuple(reasons), remaining)
