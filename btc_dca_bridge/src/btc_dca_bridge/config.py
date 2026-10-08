"""Load and validate the canonical V1 strategy definition."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .errors import ConfigurationError
from .paths import (
    CONFIG_PATH, MARKET_DATA_CONFIG_PATH, NOTIFICATIONS_CONFIG_PATH,
    PERSISTENCE_CONFIG_PATH, RESEARCH_CONFIG_PATH, RUNTIME_CONFIG_PATH,
    SENTIMENT_CONFIG_PATH,
    EXECUTION_CONFIG_PATH,
)


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
    allocation_rounding: str = ROUND_HALF_UP

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


@dataclass(frozen=True)
class ExecutionConfig:
    schema_version: str
    live_execution_enabled: bool
    kill_switch: bool
    explicit_live_approval_required: bool
    monthly_cap_usd: Decimal
    exchange: str
    market_type: str
    symbol: str
    order_submission: str


def load_execution_config(path: Path = EXECUTION_CONFIG_PATH) -> ExecutionConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load execution config {path}: {exc}") from exc
    root = _mapping(raw, "execution config")
    expected = {"schema_version", "live_execution_enabled", "kill_switch", "explicit_live_approval_required", "monthly_cap_usd", "market", "order_submission"}
    if set(root) != expected or root.get("schema_version") != "1.0.0":
        raise ConfigurationError("execution config keys or schema version are invalid")
    for key in ("live_execution_enabled", "kill_switch", "explicit_live_approval_required"):
        if not isinstance(root[key], bool): raise ConfigurationError(f"{key} must be boolean")
    cap = _decimal(root["monthly_cap_usd"], "execution.monthly_cap_usd", positive=True)
    if cap != Decimal("500") or root["market"] != "Bybit Spot BTCUSDT" or root["order_submission"] != "not_implemented":
        raise ConfigurationError("execution safety invariants are invalid")
    return ExecutionConfig(root["schema_version"], root["live_execution_enabled"], root["kill_switch"], root["explicit_live_approval_required"], cap, "Bybit", "spot", "BTCUSDT", root["order_submission"])


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
    if policy.get("rounding_tie_breaking") != "ROUND_HALF_UP":
        raise ConfigurationError("nearest_whole_usd must use ROUND_HALF_UP tie-breaking")
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
        ROUND_HALF_UP,
    )


# Operational configuration is deliberately separate from the V1 strategy
# contract above.  These models are restrictive: changing a source identity
# requires an adapter and an explicit data-source change, not just YAML edits.
OPERATIONAL_CONFIG_VERSION = "1.0.0"


@dataclass(frozen=True)
class HttpPolicy:
    connect_timeout_seconds: float
    read_timeout_seconds: float
    retry_attempts: int
    backoff_seconds: float


@dataclass(frozen=True)
class MarketDataConfig:
    config_version: str
    exchange: str
    market_type: str
    symbol: str
    primary_provider: str
    fallback_providers: tuple[str, ...]
    tradingview_external_symbol: str
    freshness_max_age_seconds: int
    tradingview_observation_max_age_seconds: int
    tradingview_timeout_seconds: float
    candle_page_limit: int
    candle_max_pages: int
    http: HttpPolicy


@dataclass(frozen=True)
class SentimentConfig:
    config_version: str
    provider: str
    index_name: str
    freshness_max_age_seconds: int
    http: HttpPolicy


@dataclass(frozen=True)
class RuntimeConfig:
    config_version: str
    timezone: str
    intended_local_time: str
    github_cron_utc: str
    scheduled_utc_hour: int
    scheduled_utc_minute: int
    minute_alignment_required: bool
    shadow_mode_enabled: bool
    live_execution_enabled: bool
    workflow_timeout_minutes: int


@dataclass(frozen=True)
class NotificationConfig:
    config_version: str
    telegram_enabled: bool
    plain_text: bool
    suppress_duplicate_success: bool
    failure_notifications_enabled: bool
    bot_token_env_var: str
    chat_id_env_var: str
    http: HttpPolicy


@dataclass(frozen=True)
class PersistenceConfig:
    config_version: str
    artifact_root: str
    completed_retention_days: int
    partial_retention_days: int
    digest_algorithm: str
    serialization_policy: str


@dataclass(frozen=True)
class ResearchConfig:
    config_version: str
    flags: dict[str, bool]


@dataclass(frozen=True)
class OperationalConfig:
    market_data: MarketDataConfig
    sentiment: SentimentConfig
    runtime: RuntimeConfig
    notifications: NotificationConfig
    persistence: PersistenceConfig
    research: ResearchConfig


def _operational_yaml(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load {label} config {path}: {exc}") from exc
    return _mapping(raw, label)


def _version(root: dict[str, Any], label: str) -> None:
    if root.get("config_version") != OPERATIONAL_CONFIG_VERSION:
        raise ConfigurationError(f"{label}.config_version must be {OPERATIONAL_CONFIG_VERSION}")


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigurationError(f"{label} must be a positive number")
    return float(value)


def _positive_int(value: Any, label: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{label} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{label} must not exceed {maximum}")
    return value


def _http_policy(root: dict[str, Any], label: str) -> HttpPolicy:
    raw = _mapping(root.get("http"), f"{label}.http")
    allowed = {"connect_timeout_seconds", "read_timeout_seconds", "retry_attempts", "backoff_seconds"}
    if set(raw) != allowed:
        raise ConfigurationError(f"{label}.http has unsupported or missing fields")
    backoff = raw["backoff_seconds"]
    if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) or backoff < 0:
        raise ConfigurationError(f"{label}.http.backoff_seconds must be non-negative")
    return HttpPolicy(
        _positive_number(raw["connect_timeout_seconds"], f"{label}.http.connect_timeout_seconds"),
        _positive_number(raw["read_timeout_seconds"], f"{label}.http.read_timeout_seconds"),
        _positive_int(raw["retry_attempts"], f"{label}.http.retry_attempts", maximum=5),
        float(backoff),
    )


def load_market_data_config(path: Path = MARKET_DATA_CONFIG_PATH) -> MarketDataConfig:
    root = _operational_yaml(path, "market_data")
    _version(root, "market_data")
    required = {"config_version", "exchange", "market_type", "symbol", "primary_provider", "fallback_providers", "tradingview_external_symbol", "freshness_max_age_seconds", "tradingview_observation_max_age_seconds", "tradingview_timeout_seconds", "candle_page_limit", "candle_max_pages", "http"}
    if set(root) != required:
        raise ConfigurationError("market_data has unsupported or missing fields")
    if root["exchange"] != "Bybit" or root["market_type"] != "spot" or root["symbol"] != "BTCUSDT":
        raise ConfigurationError("market data identity must remain Bybit BTCUSDT Spot")
    if root["primary_provider"] != "bybit_api":
        raise ConfigurationError("the canonical primary provider must be bybit_api")
    fallbacks = root["fallback_providers"]
    if fallbacks != ["tradingview"]:
        raise ConfigurationError("the only approved fallback is tradingview")
    if root["tradingview_external_symbol"] != "BYBIT:BTCUSDT" or ".P" in root["tradingview_external_symbol"]:
        raise ConfigurationError("TradingView identity must be exact BYBIT:BTCUSDT Spot")
    return MarketDataConfig(OPERATIONAL_CONFIG_VERSION, "Bybit", "spot", "BTCUSDT", "bybit_api", ("binance_api", "kucoin_api"), "BYBIT:BTCUSDT", _positive_int(root["freshness_max_age_seconds"], "market_data.freshness_max_age_seconds"), _positive_int(root["tradingview_observation_max_age_seconds"], "market_data.tradingview_observation_max_age_seconds"), _positive_number(root["tradingview_timeout_seconds"], "market_data.tradingview_timeout_seconds"), _positive_int(root["candle_page_limit"], "market_data.candle_page_limit", maximum=1000), _positive_int(root["candle_max_pages"], "market_data.candle_max_pages", maximum=100), _http_policy(root, "market_data"))


def load_sentiment_config(path: Path = SENTIMENT_CONFIG_PATH) -> SentimentConfig:
    root = _operational_yaml(path, "sentiment")
    _version(root, "sentiment")
    if set(root) != {"config_version", "provider", "index_name", "freshness_max_age_seconds", "http"}:
        raise ConfigurationError("sentiment has unsupported or missing fields")
    if root["provider"] != "alternative_me_crypto_fear_greed" or root["index_name"] != "Crypto Fear & Greed Index":
        raise ConfigurationError("the canonical sentiment source must remain Alternative.me Fear & Greed")
    return SentimentConfig(OPERATIONAL_CONFIG_VERSION, root["provider"], root["index_name"], _positive_int(root["freshness_max_age_seconds"], "sentiment.freshness_max_age_seconds"), _http_policy(root, "sentiment"))


def load_runtime_config(path: Path = RUNTIME_CONFIG_PATH) -> RuntimeConfig:
    root = _operational_yaml(path, "runtime")
    _version(root, "runtime")
    required = {"config_version", "timezone", "intended_local_time", "github_cron_utc", "scheduled_utc_hour", "scheduled_utc_minute", "minute_alignment_required", "shadow_mode_enabled", "live_execution_enabled", "workflow_timeout_minutes"}
    if set(root) != required:
        raise ConfigurationError("runtime has unsupported or missing fields")
    try:
        ZoneInfo(root["timezone"])
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise ConfigurationError("runtime.timezone must be a valid IANA timezone") from exc
    if not isinstance(root["intended_local_time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", root["intended_local_time"]):
        raise ConfigurationError("runtime.intended_local_time must be HH:MM")
    hour = root["scheduled_utc_hour"]
    minute = root["scheduled_utc_minute"]
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23 or isinstance(minute, bool) or not isinstance(minute, int) or not 0 <= minute <= 59:
        raise ConfigurationError("runtime scheduled UTC slot must be minute-aligned")
    if root["github_cron_utc"] != f"{minute} {hour} * * *":
        raise ConfigurationError("runtime.github_cron_utc must match the scheduled UTC slot")
    if root["minute_alignment_required"] is not True or root["shadow_mode_enabled"] is not True or root["live_execution_enabled"] is not False:
        raise ConfigurationError("Phase 4 runtime must be minute-aligned shadow-only with live execution disabled")
    return RuntimeConfig(OPERATIONAL_CONFIG_VERSION, root["timezone"], root["intended_local_time"], root["github_cron_utc"], hour, minute, True, True, False, _positive_int(root["workflow_timeout_minutes"], "runtime.workflow_timeout_minutes", maximum=360))


def load_notification_config(path: Path = NOTIFICATIONS_CONFIG_PATH) -> NotificationConfig:
    root = _operational_yaml(path, "notifications")
    _version(root, "notifications")
    required = {"config_version", "telegram_enabled", "plain_text", "suppress_duplicate_success", "failure_notifications_enabled", "bot_token_env_var", "chat_id_env_var", "http"}
    if set(root) != required:
        raise ConfigurationError("notifications has unsupported or missing fields")
    if any(key in root for key in ("bot_token", "chat_id", "token")):
        raise ConfigurationError("notification secrets must not be embedded in YAML")
    if root["telegram_enabled"] is not True or root["plain_text"] is not True or not all(isinstance(root[key], bool) for key in ("suppress_duplicate_success", "failure_notifications_enabled")):
        raise ConfigurationError("notifications must use enabled plain-text Telegram policy")
    for key in ("bot_token_env_var", "chat_id_env_var"):
        if not isinstance(root[key], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", root[key]):
            raise ConfigurationError(f"notifications.{key} must name an environment variable")
    return NotificationConfig(OPERATIONAL_CONFIG_VERSION, True, True, root["suppress_duplicate_success"], root["failure_notifications_enabled"], root["bot_token_env_var"], root["chat_id_env_var"], _http_policy(root, "notifications"))


def load_persistence_config(path: Path = PERSISTENCE_CONFIG_PATH) -> PersistenceConfig:
    root = _operational_yaml(path, "persistence")
    _version(root, "persistence")
    if set(root) != {"config_version", "artifact_root", "completed_retention_days", "partial_retention_days", "digest_algorithm", "serialization_policy"}:
        raise ConfigurationError("persistence has unsupported or missing fields")
    artifact_root = root["artifact_root"]
    if not isinstance(artifact_root, str) or not artifact_root or Path(artifact_root).is_absolute() or any(part in {"", ".", ".."} for part in Path(artifact_root).parts):
        raise ConfigurationError("persistence.artifact_root must be a safe relative path")
    if root["digest_algorithm"] != "sha256" or root["serialization_policy"] != "json_utf8_sorted_compact_v1":
        raise ConfigurationError("persistence digest and serialization policy are canonical")
    return PersistenceConfig(OPERATIONAL_CONFIG_VERSION, artifact_root, _positive_int(root["completed_retention_days"], "persistence.completed_retention_days", maximum=400), _positive_int(root["partial_retention_days"], "persistence.partial_retention_days", maximum=400), "sha256", "json_utf8_sorted_compact_v1")


def load_research_config(path: Path = RESEARCH_CONFIG_PATH) -> ResearchConfig:
    root = _operational_yaml(path, "research")
    _version(root, "research")
    flags = _mapping(root.get("flags"), "research.flags")
    if set(root) != {"config_version", "flags"} or not flags or not all(value is False for value in flags.values()):
        raise ConfigurationError("research flags must exist and remain disabled in Phase 4")
    return ResearchConfig(OPERATIONAL_CONFIG_VERSION, dict(flags))


def load_operational_config() -> OperationalConfig:
    return OperationalConfig(load_market_data_config(), load_sentiment_config(), load_runtime_config(), load_notification_config(), load_persistence_config(), load_research_config())
