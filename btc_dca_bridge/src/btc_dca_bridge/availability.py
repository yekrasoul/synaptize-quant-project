"""Single fail-closed policy for authoritative Spot quote-buy availability."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import FrozenSet

from .private_bybit import SpotQuoteAvailability


class AvailabilityValidationError(ValueError):
    """Availability is missing, untrusted, stale, or outside the policy."""


@dataclass(frozen=True)
class SpotQuoteAvailabilityPolicy:
    approved_sources: FrozenSet[tuple[str, str]]
    supported_account_types: FrozenSet[str]
    max_age: timedelta = timedelta(seconds=60)
    future_tolerance: timedelta = timedelta(seconds=5)


APPROVED_SPOT_QUOTE_AVAILABILITY_SOURCES = frozenset({
    ("/v5/order/spot-borrow-check", "spotMaxTradeAmount"),
})
PRODUCTION_AVAILABILITY_POLICY = SpotQuoteAvailabilityPolicy(
    approved_sources=APPROVED_SPOT_QUOTE_AVAILABILITY_SOURCES,
    supported_account_types=frozenset({"UNIFIED"}),
)


def validate_spot_quote_availability(
    value: object,
    *,
    now: datetime,
    policy: SpotQuoteAvailabilityPolicy = PRODUCTION_AVAILABILITY_POLICY,
) -> Decimal:
    """Return the trusted amount or raise; plain Decimal values never pass."""
    if not isinstance(value, SpotQuoteAvailability):
        raise AvailabilityValidationError("Spot quote availability must carry explicit provenance")
    try:
        amount = Decimal(str(value.amount_usdt))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AvailabilityValidationError("availability amount is malformed") from exc
    if not amount.is_finite() or amount < 0:
        raise AvailabilityValidationError("availability amount must be finite and non-negative")
    if value.authoritative is not True:
        raise AvailabilityValidationError("availability is not marked authoritative")
    if (value.source_endpoint, value.source_field) not in policy.approved_sources:
        raise AvailabilityValidationError("availability source is not approved for the exact operation")
    if value.account_type not in policy.supported_account_types:
        raise AvailabilityValidationError("availability account type is unsupported")
    try:
        observed = datetime.fromisoformat(str(value.observed_at_utc).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AvailabilityValidationError("availability timestamp is malformed") from exc
    if observed.tzinfo is None or observed.utcoffset() != timedelta(0):
        raise AvailabilityValidationError("availability timestamp must be explicit UTC")
    observed = observed.astimezone(UTC)
    current = now.astimezone(UTC)
    if observed > current + policy.future_tolerance:
        raise AvailabilityValidationError("availability timestamp is future-dated")
    if current - observed > policy.max_age:
        raise AvailabilityValidationError("availability timestamp is stale")
    return amount
