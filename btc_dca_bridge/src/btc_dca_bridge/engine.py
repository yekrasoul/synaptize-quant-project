"""Pure deterministic BTC Adaptive DCA V1 calculation."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import StrategyConfig
from .errors import InputValidationError
from .models import MarketSnapshot, StrategyDecision


_SNAPSHOT_ID = re.compile(r"^market_[A-Za-z0-9_-]+$")


def _parse_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise InputValidationError(f"{label} must be an RFC 3339 date-time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputValidationError(f"{label} must be an RFC 3339 date-time") from exc
    if parsed.tzinfo is None:
        raise InputValidationError(f"{label} must include a UTC offset")
    if parsed.utcoffset() != timedelta(0):
        raise InputValidationError(f"{label} must be UTC")
    return parsed


def _input_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise InputValidationError(f"{label} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InputValidationError(f"{label} must be numeric") from exc
    if not result.is_finite():
        raise InputValidationError(f"{label} must be finite")
    return result


def validate_calculation_inputs(
    snapshot: MarketSnapshot,
    fear_greed_index: int,
    monthly_spent: Any,
    strategy: StrategyConfig,
) -> Decimal:
    if snapshot.schema_version != "1.0.0":
        raise InputValidationError("unexpected MarketSnapshot schema version")
    if not _SNAPSHOT_ID.fullmatch(snapshot.snapshot_id):
        raise InputValidationError("invalid MarketSnapshot ID")
    if (
        snapshot.source_exchange != "Bybit"
        or snapshot.market_type != "spot"
        or snapshot.symbol != "BTCUSDT"
    ):
        raise InputValidationError("MarketSnapshot must represent Bybit BTCUSDT Spot")
    if not all(
        value is True
        for value in (
            snapshot.same_source_price_and_high,
            snapshot.full_168h_coverage,
            snapshot.fresh,
        )
    ):
        raise InputValidationError("MarketSnapshot validation flags must all be true")
    _parse_datetime(snapshot.captured_at_utc, "captured_at_utc")
    window_start = _parse_datetime(snapshot.window_start_utc, "window_start_utc")
    window_end = _parse_datetime(snapshot.window_end_utc, "window_end_utc")
    if (window_end - window_start).total_seconds() != 168 * 60 * 60:
        raise InputValidationError("MarketSnapshot window must cover exactly 168 hours")
    price = _input_decimal(snapshot.current_price_usdt, "current price")
    high = _input_decimal(snapshot.rolling_7d_high_usdt, "7D high")
    if price <= 0:
        raise InputValidationError("current price must be greater than zero")
    if high <= 0:
        raise InputValidationError("7D high must be greater than zero")
    if price > high:
        raise InputValidationError("current price cannot exceed its rolling 7D high")
    if isinstance(fear_greed_index, bool) or not isinstance(fear_greed_index, int):
        raise InputValidationError("Fear & Greed index must be an integer")
    if not 0 <= fear_greed_index <= 100:
        raise InputValidationError("Fear & Greed index must be within 0..100")
    spent = _input_decimal(monthly_spent, "monthly spend")
    if spent < 0 or spent > strategy.monthly_cap_usd:
        raise InputValidationError(
            f"monthly spend must be within 0..{strategy.monthly_cap_usd}"
        )
    return spent


def calculate_drawdown(price: Any, high_7d: Any) -> Decimal:
    current = _input_decimal(price, "current price")
    high = _input_decimal(high_7d, "7D high")
    if current <= 0 or high <= 0:
        raise InputValidationError("current price and 7D high must be greater than zero")
    if current > high:
        raise InputValidationError("current price cannot exceed its rolling 7D high")
    return (current - high) / high * Decimal(100)


def _json_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def calculate_decision(
    snapshot: MarketSnapshot,
    fear_greed_index: int,
    monthly_spent: Any,
    strategy: StrategyConfig,
) -> StrategyDecision:
    """Return a schema-valid decision without I/O or mutable state."""
    spent = validate_calculation_inputs(
        snapshot, fear_greed_index, monthly_spent, strategy
    )
    drawdown = calculate_drawdown(
        snapshot.current_price_usdt, snapshot.rolling_7d_high_usdt
    )
    base = strategy.base_allocation(drawdown)
    multiplier = strategy.sentiment_multiplier(fear_greed_index)
    rounded = (base * multiplier).quantize(
        Decimal("1"), rounding=strategy.allocation_rounding
    )
    calculated = max(rounded, strategy.minimum_purchase_usd)
    remaining = strategy.monthly_cap_usd - spent

    if remaining < strategy.minimum_purchase_usd:
        final = Decimal(0)
        status = "monthly_cap_reached"
        reason = "remaining monthly budget is below the minimum purchase"
    elif calculated <= remaining:
        final = calculated
        status = "approved"
        reason = "calculated allocation is within the remaining monthly budget"
    else:
        final = remaining.to_integral_value(rounding="ROUND_FLOOR")
        status = "approved"
        reason = "allocation clipped down to the largest whole USD within the monthly cap"

    identity = {
        "snapshot": snapshot.to_dict(),
        "fear_greed_index": fear_greed_index,
        "monthly_spent": str(spent),
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy.strategy_version,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    payload = {
        "schema_version": "1.0.0",
        "decision_id": f"decision_{digest}",
        "created_at_utc": snapshot.captured_at_utc,
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy.strategy_version,
        "market_snapshot_id": snapshot.snapshot_id,
        "drawdown_percent": float(drawdown),
        "fear_greed_index": fear_greed_index,
        "base_allocation_usd": int(base),
        "sentiment_multiplier": float(multiplier),
        "calculated_allocation_usd": int(calculated),
        "monthly_spent_before_usd": _json_number(spent),
        "remaining_budget_before_usd": _json_number(remaining),
        "final_purchase_usd": int(final),
        "status": status,
        "reason": reason,
    }
    return StrategyDecision(payload)
