"""Load and validate the canonical V1 strategy definition."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigurationError
from .paths import CONFIG_PATH


EXPECTED_STRATEGY_ID = "btc_adaptive_dca_v1"
EXPECTED_STRATEGY_VERSION = "1.0.0"
EXPECTED_CAP_ORDER = (
    "calculate_base_purchase",
    "apply_sentiment_multiplier",
    "round_nearest_whole_usd",
    "enforce_minimum_purchase_usd",
    "enforce_remaining_calendar_month_budget",
)
_CLAUSE = re.compile(r"^drawdown_percent\s*(>=|>|<=|<)\s*(-?\d+(?:\.\d+)?)$")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a mapping")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{label} must be a non-empty list")
    return value


def _decimal(value: Any, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ConfigurationError(f"{label} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigurationError(f"{label} must be numeric") from exc
    if not result.is_finite() or (positive and result <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise ConfigurationError(f"{label} must be {qualifier}")
    return result


@dataclass(frozen=True)
class DrawdownBand:
    condition: str
    purchase_usd: Decimal
    clauses: tuple[tuple[str, Decimal], ...]

    def matches(self, drawdown: Decimal) -> bool:
        comparisons = {
            ">=": lambda left, right: left >= right,
            ">": lambda left, right: left > right,
            "<=": lambda left, right: left <= right,
            "<": lambda left, right: left < right,
        }
        return all(comparisons[operator](drawdown, limit) for operator, limit in self.clauses)


@dataclass(frozen=True)
class SentimentBand:
    minimum: int
    maximum: int
    multiplier: Decimal


@dataclass(frozen=True)
class StrategyConfig:
    strategy_id: str
    strategy_version: str
    core_daily_usd: Decimal
    minimum_purchase_usd: Decimal
    monthly_cap_usd: Decimal
    drawdown_bands: tuple[DrawdownBand, ...]
    sentiment_bands: tuple[SentimentBand, ...]

    def base_allocation(self, drawdown: Decimal) -> Decimal:
        for band in self.drawdown_bands:
            if band.matches(drawdown):
                return band.purchase_usd
        raise ConfigurationError(f"drawdown bands do not cover {drawdown}")

    def sentiment_multiplier(self, index: int) -> Decimal:
        for band in self.sentiment_bands:
            if band.minimum <= index <= band.maximum:
                return band.multiplier
        raise ConfigurationError(f"sentiment bands do not cover {index}")


def _parse_condition(condition: Any, label: str) -> DrawdownBand:
    if not isinstance(condition, str) or not condition:
        raise ConfigurationError(f"{label}.condition must be a non-empty string")
    clauses: list[tuple[str, Decimal]] = []
    for raw_clause in condition.split(" and "):
        match = _CLAUSE.fullmatch(raw_clause.strip())
        if not match:
            raise ConfigurationError(f"unsupported {label}.condition: {condition!r}")
        clauses.append((match.group(1), Decimal(match.group(2))))
    return DrawdownBand(condition, Decimal(0), tuple(clauses))


def _validate_drawdown_coverage(bands: tuple[DrawdownBand, ...]) -> None:
    boundaries = {limit for band in bands for _, limit in band.clauses if limit <= 0}
    points = {Decimal(0), min(boundaries, default=Decimal(0)) - 1}
    epsilon = Decimal("0.000000001")
    for boundary in boundaries:
        points.update((boundary - epsilon, boundary, min(Decimal(0), boundary + epsilon)))
    for point in points:
        matches = sum(band.matches(point) for band in bands)
        if matches != 1:
            raise ConfigurationError(
                f"drawdown bands must cover every non-positive value exactly once; "
                f"{point} has {matches} matches"
            )


def load_strategy_config(path: Path = CONFIG_PATH) -> StrategyConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load strategy config {path}: {exc}") from exc
    root = _mapping(raw, "config")
    strategy = _mapping(root.get("strategy"), "strategy")
    if strategy.get("id") != EXPECTED_STRATEGY_ID:
        raise ConfigurationError(f"unexpected strategy identity: {strategy.get('id')!r}")
    if str(strategy.get("version")) != EXPECTED_STRATEGY_VERSION:
        raise ConfigurationError(f"unexpected strategy version: {strategy.get('version')!r}")
    if strategy.get("status") != "active" or strategy.get("asset") != "BTC":
        raise ConfigurationError("strategy must be active BTC V1")
    if strategy.get("quote_currency") != "USDT":
        raise ConfigurationError("strategy quote currency must be USDT")

    market = _mapping(root.get("market_policy"), "market_policy")
    if (
        market.get("primary_exchange") != "Bybit"
        or market.get("market_type") != "spot"
        or market.get("symbol") != "BTCUSDT"
    ):
        raise ConfigurationError("V1 market policy must be Bybit BTCUSDT Spot")
    if market.get("same_source_price_and_high_required") is not True:
        raise ConfigurationError("price and rolling high must use the same source")

    capital = _mapping(root.get("capital_rules"), "capital_rules")
    core = _decimal(capital.get("core_daily_usd"), "core_daily_usd", positive=True)
    minimum = _decimal(
        capital.get("minimum_purchase_usd"), "minimum_purchase_usd", positive=True
    )
    cap = _decimal(capital.get("monthly_cap_usd"), "monthly_cap_usd", positive=True)
    if any(value != value.to_integral_value() for value in (core, minimum, cap)):
        raise ConfigurationError("V1 capital rule amounts must be whole USD")
    if minimum > cap:
        raise ConfigurationError("minimum purchase cannot exceed monthly cap")
    if capital.get("borrowing_allowed") is not False or capital.get("leverage_allowed") is not False:
        raise ConfigurationError("V1 borrowing and leverage must remain disabled")
    if capital.get("unused_monthly_budget_carry_forward") is not False:
        raise ConfigurationError("V1 budget carry-forward must remain disabled")

    drawdown = _mapping(root.get("drawdown"), "drawdown")
    if drawdown.get("reference") != "rolling_high" or drawdown.get("window_hours") != 168:
        raise ConfigurationError("V1 requires a 168-hour rolling high")
    if drawdown.get("formula") != "(current_price_usdt - rolling_7d_high_usdt) / rolling_7d_high_usdt * 100":
        raise ConfigurationError("unexpected V1 drawdown formula")
    if drawdown.get("band_resolution") != "first_matching_rule_in_list":
        raise ConfigurationError("unsupported drawdown band resolution")
    parsed_bands: list[DrawdownBand] = []
    for position, raw_band in enumerate(
        _list(drawdown.get("base_purchase_bands"), "base_purchase_bands")
    ):
        band = _mapping(raw_band, f"base_purchase_bands[{position}]")
        parsed = _parse_condition(band.get("condition"), f"base_purchase_bands[{position}]")
        purchase = _decimal(band.get("purchase_usd"), "purchase_usd", positive=True)
        if purchase != purchase.to_integral_value():
            raise ConfigurationError("base purchase amounts must be whole USD")
        parsed_bands.append(
            DrawdownBand(
                parsed.condition,
                purchase,
                parsed.clauses,
            )
        )
    drawdown_bands = tuple(parsed_bands)
    _validate_drawdown_coverage(drawdown_bands)

    sentiment = _mapping(root.get("sentiment"), "sentiment")
    if sentiment.get("role") != "secondary_adjustment_only":
        raise ConfigurationError("sentiment must remain a secondary adjustment only")
    sentiment_bands: list[SentimentBand] = []
    covered: set[int] = set()
    for position, raw_band in enumerate(
        _list(sentiment.get("multiplier_bands"), "multiplier_bands")
    ):
        band = _mapping(raw_band, f"multiplier_bands[{position}]")
        low, high = band.get("min_index"), band.get("max_index")
        if isinstance(low, bool) or isinstance(high, bool) or not isinstance(low, int) or not isinstance(high, int):
            raise ConfigurationError("sentiment band limits must be integers")
        if low < 0 or high > 100 or low > high:
            raise ConfigurationError("sentiment band limits must be ordered within 0..100")
        values = set(range(low, high + 1))
        if covered & values:
            raise ConfigurationError("sentiment multiplier bands overlap")
        covered |= values
        sentiment_bands.append(
            SentimentBand(
                low,
                high,
                _decimal(band.get("multiplier"), "multiplier", positive=True),
            )
        )
    if covered != set(range(101)):
        raise ConfigurationError("sentiment multiplier bands must cover every index 0..100")

    policy = _mapping(root.get("calculation_policy"), "calculation_policy")
    if policy.get("rounding") != "nearest_whole_usd":
        raise ConfigurationError("unsupported allocation rounding policy")
    if policy.get("preserve_minimum_purchase_usd_after_sentiment") is not True:
        raise ConfigurationError("V1 minimum preservation must remain enabled")
    if tuple(policy.get("cap_application_order", ())) != EXPECTED_CAP_ORDER:
        raise ConfigurationError("unexpected cap application order")
    if _decimal(policy.get("when_remaining_budget_below_minimum_usd"), "below-minimum result") != 0:
        raise ConfigurationError("below-minimum remaining budget must produce zero")
    if policy.get("when_calculated_allocation_exceeds_remaining_budget") != "floor_remaining_budget_to_whole_usd":
        raise ConfigurationError("missing approved fractional-budget clipping policy")

    execution = _mapping(root.get("execution_policy"), "execution_policy")
    if execution.get("mode") != "recommendation_only" or execution.get("live_order_submission") != "prohibited":
        raise ConfigurationError("V1 must remain recommendation-only")

    return StrategyConfig(
        EXPECTED_STRATEGY_ID,
        EXPECTED_STRATEGY_VERSION,
        core,
        minimum,
        cap,
        drawdown_bands,
        tuple(sentiment_bands),
    )
