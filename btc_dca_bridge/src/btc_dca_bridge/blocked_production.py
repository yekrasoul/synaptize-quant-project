"""Deterministic reporting for the intentionally blocked production state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from .private_bybit import BybitPrivateReadClient
from .quote_limits import (
    PRODUCTION_QUOTE_UNIT_LIMIT_POLICY,
    QuoteUnitLimitEvidence,
    QuoteUnitLimitPolicy,
    QuoteUnitLimitValidationError,
    unavailable_quote_unit_limit,
    validate_quote_unit_limit_evidence,
)

QUOTE_LIMIT_BLOCKER_ID = "BYBIT_QUOTE_UNIT_MAX_NOT_EXPOSED"
_USE_PRODUCTION_EVIDENCE = object()
QUOTE_LIMIT_BLOCKED_MESSAGE = (
    "External Bybit contract blocker: authoritative quote-unit maximum for "
    "BTCUSDT Spot Market Buy with marketUnit=quoteCoin is not exposed. "
    "No safe workaround is approved. Production execution remains disabled."
)


def _utc(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("blocked-production timestamps must be UTC-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ProductionBlocker:
    blocker_id: str
    category: str
    severity: str
    source: str
    status: str
    first_observed_at_utc: str
    last_observed_at_utc: str
    retryable: bool
    external_dependency: bool
    remediation: str
    evidence: str
    authorization_impact: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ExchangeContractCapabilities:
    exchange: str
    market_type: str
    symbol: str
    spot_quote_availability_source: str | None
    spot_quote_availability_supported: bool
    quote_unit_maximum_source: str | None
    quote_unit_maximum_supported: bool
    market_unit_quote_coin_supported: bool
    instrument_minimum_supported: bool
    account_mode_supported: bool
    verified_at_utc: str
    documentation_version: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExchangeContractCapabilities":
        fields = {field: value[field] for field in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass(frozen=True)
class ContractDrift:
    result: str
    changed_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"result": self.result, "changed_fields": list(self.changed_fields)}


def _production_quote_limit_evidence() -> QuoteUnitLimitEvidence:
    # The current provider reports NOT_EXPOSED locally; it performs no request.
    try:
        return BybitPrivateReadClient.quote_unit_limit_evidence()
    except Exception:
        return unavailable_quote_unit_limit(observed_at_utc=_utc())


def _validated_quote_limit(
    *,
    evidence: object,
    policy: QuoteUnitLimitPolicy,
    now: datetime,
) -> tuple[bool, str | None]:
    try:
        validate_quote_unit_limit_evidence(evidence, now=now, policy=policy)
    except (QuoteUnitLimitValidationError, TypeError, ValueError):
        return False, None
    assert isinstance(evidence, QuoteUnitLimitEvidence)
    return True, f"{evidence.source_endpoint}:{evidence.source_field}"


def current_contract_capabilities(
    *,
    now: datetime | None = None,
    quote_limit_policy: QuoteUnitLimitPolicy = PRODUCTION_QUOTE_UNIT_LIMIT_POLICY,
    quote_limit_evidence: object = _USE_PRODUCTION_EVIDENCE,
) -> ExchangeContractCapabilities:
    current = now or datetime.now(UTC)
    evidence = _production_quote_limit_evidence() if quote_limit_evidence is _USE_PRODUCTION_EVIDENCE else quote_limit_evidence
    quote_supported, quote_source = _validated_quote_limit(
        evidence=evidence, policy=quote_limit_policy, now=current
    )
    return ExchangeContractCapabilities(
        exchange="Bybit",
        market_type="spot",
        symbol="BTCUSDT",
        spot_quote_availability_source="/v5/order/spot-borrow-check:spotMaxTradeAmount",
        spot_quote_availability_supported=True,
        quote_unit_maximum_source=quote_source,
        quote_unit_maximum_supported=quote_supported,
        market_unit_quote_coin_supported=True,
        instrument_minimum_supported=True,
        account_mode_supported=True,
        verified_at_utc=_utc(now),
        documentation_version="Phase 5.8 reviewed Bybit V5 contract; verified 2026-10-07",
    )


def active_production_blockers(
    *,
    now: datetime | None = None,
    quote_limit_policy: QuoteUnitLimitPolicy = PRODUCTION_QUOTE_UNIT_LIMIT_POLICY,
    quote_limit_evidence: object = _USE_PRODUCTION_EVIDENCE,
) -> tuple[ProductionBlocker, ...]:
    current = now or datetime.now(UTC)
    evidence = _production_quote_limit_evidence() if quote_limit_evidence is _USE_PRODUCTION_EVIDENCE else quote_limit_evidence
    supported, _ = _validated_quote_limit(
        evidence=evidence, policy=quote_limit_policy, now=current
    )
    if supported:
        return ()
    timestamp = _utc(current)
    return (ProductionBlocker(
        blocker_id=QUOTE_LIMIT_BLOCKER_ID,
        category="MARKET_CONTRACT",
        severity="BLOCKING",
        source="Bybit V5 Spot contract",
        status="ACTIVE",
        first_observed_at_utc=timestamp,
        last_observed_at_utc=timestamp,
        retryable=False,
        external_dependency=True,
        remediation="Obtain and manually review an official authoritative quote-unit maximum; update the shared validator and production policy through a reviewed PR, then recollect evidence.",
        evidence=QUOTE_LIMIT_BLOCKED_MESSAGE,
        authorization_impact="BLOCKS_REAL_MONEY",
    ),)


def compare_contract_capabilities(previous: ExchangeContractCapabilities | Mapping[str, Any], current: ExchangeContractCapabilities | Mapping[str, Any]) -> ContractDrift:
    try:
        before = previous if isinstance(previous, ExchangeContractCapabilities) else ExchangeContractCapabilities.from_mapping(previous)
        after = current if isinstance(current, ExchangeContractCapabilities) else ExchangeContractCapabilities.from_mapping(current)
    except (KeyError, TypeError, ValueError):
        return ContractDrift("AMBIGUOUS_CHANGE")
    changed = tuple(field for field in before.__dataclass_fields__ if field not in {"verified_at_utc", "documentation_version"} and getattr(before, field) != getattr(after, field))
    if not changed:
        return ContractDrift("NO_CHANGE")
    capability_fields = {"spot_quote_availability_supported", "quote_unit_maximum_supported", "market_unit_quote_coin_supported", "instrument_minimum_supported", "account_mode_supported"}
    if len(changed) == 1 and changed[0] in capability_fields:
        field = changed[0]
        if not getattr(before, field) and getattr(after, field):
            return ContractDrift("CAPABILITY_ADDED", changed)
        if getattr(before, field) and not getattr(after, field):
            return ContractDrift("CAPABILITY_REMOVED", changed)
    return ContractDrift("CONTRACT_CHANGED", changed)


def contract_status(
    *,
    now: datetime | None = None,
    quote_limit_policy: QuoteUnitLimitPolicy = PRODUCTION_QUOTE_UNIT_LIMIT_POLICY,
    quote_limit_evidence: object = _USE_PRODUCTION_EVIDENCE,
) -> dict[str, Any]:
    evidence = _production_quote_limit_evidence() if quote_limit_evidence is _USE_PRODUCTION_EVIDENCE else quote_limit_evidence
    capabilities = current_contract_capabilities(
        now=now, quote_limit_policy=quote_limit_policy, quote_limit_evidence=evidence
    )
    conclusion = "QUOTE_UNIT_MAX_CONFIRMED" if capabilities.quote_unit_maximum_supported else "QUOTE_UNIT_MAX_NOT_EXPOSED"
    return {
        "capabilities": capabilities.to_dict(),
        "production_quote_limit_conclusion": conclusion,
        "approved_quote_unit_limit_source_count": len(quote_limit_policy.approved_sources),
        "create_order_assumptions": {"category": "spot", "symbol": "BTCUSDT", "orderType": "Market", "marketUnit": "quoteCoin", "isLeverage": 0, "orderFilter": "Order"},
        "instrument_metadata_assumptions": {"source": "/v5/market/instruments-info", "base_quantity_max_is_not_quote_max": True},
        "account_mode_assumptions": {"unifiedMarginStatus": 6, "marginMode": "REGULAR_MARGIN", "spotHedgingStatus": "OFF"},
        "last_research_verification_date": "2026-10-07",
        "real_money_authorization": {"granted": False, "source": "none", "required": True, "status": "NOT_AUTHORIZED"},
    }
