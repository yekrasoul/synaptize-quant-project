"""Fail-closed evidence model for Spot quote-unit market-buy limits."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import FrozenSet


class QuoteUnitLimitValidationError(ValueError):
    """Quote-unit limit evidence is absent, stale, or not authoritative."""


@dataclass(frozen=True)
class QuoteUnitLimitPolicy:
    approved_sources: FrozenSet[tuple[str, str]]
    max_age: timedelta = timedelta(minutes=10)
    future_tolerance: timedelta = timedelta(seconds=5)


APPROVED_QUOTE_UNIT_LIMIT_SOURCES = frozenset()
PRODUCTION_QUOTE_UNIT_LIMIT_POLICY = QuoteUnitLimitPolicy(APPROVED_QUOTE_UNIT_LIMIT_SOURCES)


@dataclass(frozen=True)
class QuoteUnitLimitEvidence:
    symbol: str
    market_type: str
    side: str
    order_type: str
    market_unit: str
    quote_currency: str
    source_endpoint: str
    source_field: str
    unit: str
    maximum_quote_usdt: Decimal | None
    authoritative: bool
    observed_at_utc: str
    conclusion: str

    @classmethod
    def from_mapping(cls, value: object) -> "QuoteUnitLimitEvidence":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise QuoteUnitLimitValidationError("quote-unit limit evidence is not an object")
        try:
            maximum = value.get("maximum_quote_usdt")
            return cls(**{**value, "maximum_quote_usdt": None if maximum is None else Decimal(str(maximum))})
        except (TypeError, KeyError, InvalidOperation, ValueError) as exc:
            raise QuoteUnitLimitValidationError("quote-unit limit evidence is malformed") from exc

    def to_dict(self) -> dict[str, object]:
        return {**self.__dict__, "maximum_quote_usdt": None if self.maximum_quote_usdt is None else str(self.maximum_quote_usdt)}


def validate_quote_unit_limit_evidence(
    value: object,
    *,
    now: datetime,
    policy: QuoteUnitLimitPolicy = PRODUCTION_QUOTE_UNIT_LIMIT_POLICY,
) -> Decimal:
    """Return a trusted quote ceiling; no conversion or base-unit fallback is allowed."""
    if not isinstance(value, QuoteUnitLimitEvidence):
        raise QuoteUnitLimitValidationError("quote-unit limit must carry explicit evidence")
    if (value.symbol, value.market_type, value.side, value.order_type, value.market_unit, value.quote_currency, value.unit) != (
        "BTCUSDT", "spot", "Buy", "Market", "quoteCoin", "USDT", "USDT"
    ):
        raise QuoteUnitLimitValidationError("quote-unit limit identity or unit is not the approved Spot operation")
    if value.conclusion != "CONFIRMED" or value.authoritative is not True:
        raise QuoteUnitLimitValidationError("quote-unit maximum is not authoritatively confirmed")
    if (value.source_endpoint, value.source_field) not in policy.approved_sources:
        raise QuoteUnitLimitValidationError("quote-unit maximum source is not approved")
    try:
        maximum = Decimal(str(value.maximum_quote_usdt))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise QuoteUnitLimitValidationError("quote-unit maximum is malformed") from exc
    if not maximum.is_finite() or maximum <= 0:
        raise QuoteUnitLimitValidationError("quote-unit maximum must be finite and positive")
    try:
        observed = datetime.fromisoformat(str(value.observed_at_utc).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise QuoteUnitLimitValidationError("quote-unit evidence timestamp is malformed") from exc
    if observed.tzinfo is None or observed.utcoffset() != timedelta(0):
        raise QuoteUnitLimitValidationError("quote-unit evidence timestamp must be explicit UTC")
    current = now.astimezone(UTC)
    observed = observed.astimezone(UTC)
    if observed > current + policy.future_tolerance:
        raise QuoteUnitLimitValidationError("quote-unit evidence is future-dated")
    if current - observed > policy.max_age:
        raise QuoteUnitLimitValidationError("quote-unit evidence is stale")
    return maximum


def unavailable_quote_unit_limit(*, observed_at_utc: str) -> QuoteUnitLimitEvidence:
    return QuoteUnitLimitEvidence(
        "BTCUSDT", "spot", "Buy", "Market", "quoteCoin", "USDT",
        "/v5/market/instruments-info", "lotSizeFilter.maxMarketOrderQty", "baseCoin",
        None, False, observed_at_utc, "NOT_EXPOSED",
    )
