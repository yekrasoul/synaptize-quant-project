"""Validated 168-hour Bybit Spot rolling-high construction.

The mathematical interval is closed: ``[window_start, window_end]``. Klines
are half-open aggregate observations. For minute-aligned ends, complete hourly
klines cover the interior and one-minute klines cover only the partial-hour
boundaries. Seconds cannot be clipped from a one-minute aggregate, so they fail
closed rather than being rounded.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ..errors import (
    ContradictoryPriceDataError,
    DataStaleError,
    InsufficientHistoryError,
    InvalidMarketIdentityError,
    InvalidResponseError,
    InvalidWindowError,
    SourceMismatchError,
)
from ..models import MarketSnapshot
from ..schemas import validate_artifact
from .bybit import BTCUSDT_SYMBOL, BYBIT_EXCHANGE, BYBIT_SOURCE, HOURLY_INTERVAL_MINUTES, MINUTE_INTERVAL_MINUTES, SPOT_MARKET
from .models import Candle, CandleHistory, Ticker


ROLLING_WINDOW_HOURS = 168
ROLLING_WINDOW = timedelta(hours=ROLLING_WINDOW_HOURS)
DEFAULT_MAX_INPUT_AGE = timedelta(minutes=5)
MAX_SOURCE_CLOCK_SKEW = timedelta(seconds=5)
TRADINGVIEW_SOURCE = "tradingview"
EXACT_EXTERNAL_SYMBOL = "BYBIT:BTCUSDT"


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise InvalidResponseError(f"{label} must be a UTC-aware datetime")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _price(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise InvalidResponseError(f"{label} must be a positive finite Decimal")


def _floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _ceil_hour(value: datetime) -> datetime:
    floor = _floor_hour(value)
    return floor if floor == value else floor + timedelta(hours=1)


def _validate_identity(ticker: Ticker, *histories: CandleHistory | None) -> None:
    values = [ticker]
    for history in histories:
        if history is not None:
            values.extend((history, *history.candles))
    if any(value.market != SPOT_MARKET for value in values):
        raise InvalidMarketIdentityError("rolling-high inputs must all identify Bybit Spot")
    if any(value.exchange != BYBIT_EXCHANGE or value.symbol != BTCUSDT_SYMBOL for value in values):
        raise SourceMismatchError("rolling-high inputs must all identify Bybit BTCUSDT")
    source = ticker.source
    if source not in {BYBIT_SOURCE, TRADINGVIEW_SOURCE}:
        raise SourceMismatchError(f"unsupported rolling-high source {source!r}")
    if any(value.source != source for value in values):
        raise SourceMismatchError("rolling-high inputs must all use one source path")
    if source == TRADINGVIEW_SOURCE:
        if any(value.external_symbol != EXACT_EXTERNAL_SYMBOL for value in values):
            raise InvalidMarketIdentityError(
                "TradingView inputs must all prove exact BYBIT:BTCUSDT identity"
            )
    elif any(
        value.external_symbol not in {"", EXACT_EXTERNAL_SYMBOL} for value in values
    ):
        raise SourceMismatchError("Bybit inputs contain contradictory external symbols")


def _validate_freshness(
    ticker: Ticker,
    histories: tuple[CandleHistory | None, ...],
    captured_at: datetime,
    max_input_age: timedelta,
    max_observation_age: timedelta,
) -> None:
    if not isinstance(max_input_age, timedelta) or max_input_age <= timedelta(0):
        raise ValueError("max_input_age must be greater than zero")
    if not isinstance(max_observation_age, timedelta) or max_observation_age <= timedelta(0):
        raise ValueError("max_observation_age must be greater than zero")
    values = [("ticker", _utc(ticker.retrieved_at_utc, "ticker retrieved_at_utc"))]
    values.extend(("candle history", _utc(history.retrieved_at_utc, "history retrieved_at_utc")) for history in histories if history is not None)
    for label, retrieved in values:
        if retrieved > captured_at:
            raise InvalidResponseError(f"{label} retrieval time is after capture time")
        if captured_at - retrieved > max_input_age:
            raise DataStaleError(f"{label} is older than the allowed {max_input_age.total_seconds():g} seconds")
    if ticker.exchange_response_time_utc is not None:
        response = _utc(ticker.exchange_response_time_utc, "ticker exchange_response_time_utc")
        retrieved = values[0][1]
        if response > retrieved + MAX_SOURCE_CLOCK_SKEW:
            raise InvalidResponseError("ticker exchange response time is after local retrieval time")
        if retrieved - response > max_input_age:
            raise DataStaleError("ticker exchange response is stale")
    if ticker.observed_at_utc is not None:
        observed = _utc(ticker.observed_at_utc, "ticker observed_at_utc")
        if observed > values[0][1] + MAX_SOURCE_CLOCK_SKEW:
            raise InvalidResponseError("ticker observation is after retrieval time")
        if values[0][1] - observed > max_observation_age:
            raise DataStaleError("ticker observation is stale")


def _validate_candle(candle: Candle, interval_minutes: int) -> None:
    opened = _utc(candle.open_time_utc, "candle open_time_utc")
    if candle.interval_minutes != interval_minutes or opened.second or opened.microsecond or opened.minute % interval_minutes:
        raise InvalidResponseError("candle does not match its declared interval")
    for label, value in (("open", candle.open), ("high", candle.high), ("low", candle.low), ("close", candle.close)):
        _price(value, f"candle {label}")
    if candle.low > candle.high or not all(candle.low <= value <= candle.high for value in (candle.open, candle.close)):
        raise ContradictoryPriceDataError("candle contains contradictory OHLC values")


def _required(history: CandleHistory | None, *, start: datetime, end: datetime, interval_minutes: int, label: str) -> tuple[Candle, ...]:
    if start == end:
        return ()
    if history is None or history.complete is not True or history.interval_minutes != interval_minutes:
        raise InsufficientHistoryError(f"missing or incomplete {label} history")
    if _utc(history.requested_start_utc, "history requested_start_utc") > start or _utc(history.requested_end_utc, "history requested_end_utc") < end:
        raise InsufficientHistoryError(f"{label} request does not span required coverage")
    if _utc(history.coverage_start_utc, "history coverage_start_utc") > start or _utc(history.coverage_end_utc, "history coverage_end_utc") < end:
        raise InsufficientHistoryError(f"{label} coverage does not span required coverage")
    for candle in history.candles:
        _validate_candle(candle, interval_minutes)
    selected = tuple(candle for candle in history.candles if candle.open_time_utc >= start and candle.close_time_exclusive_utc <= end)
    expected = tuple(start + timedelta(minutes=interval_minutes * index) for index in range(int((end - start).total_seconds() // (interval_minutes * 60))))
    if tuple(candle.open_time_utc for candle in selected) != expected or len({candle.open_time_utc for candle in history.candles}) != len(history.candles):
        raise InsufficientHistoryError(f"{label} history is missing, duplicate, or out of order")
    return selected


def _snapshot_id(source: str, captured_at: datetime, start: datetime, end: datetime, price: Decimal, high: Decimal) -> str:
    material = "|".join((source, BYBIT_EXCHANGE, SPOT_MARKET, BTCUSDT_SYMBOL, _iso(captured_at), _iso(start), _iso(end), str(price), str(high)))
    return f"market_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def build_market_snapshot(ticker: Ticker, history: CandleHistory, *, start_boundary_history: CandleHistory | None = None, end_boundary_history: CandleHistory | None = None, captured_at_utc: datetime | None = None, max_input_age: timedelta = DEFAULT_MAX_INPUT_AGE, max_observation_age: timedelta | None = None) -> MarketSnapshot:
    """Build a schema-valid snapshot at the source's proven candle resolution.

    Arbitrary minute-aligned wall-clock ends are supported. Any non-zero seconds
    or microseconds fail because the one-minute aggregate high cannot be
    attributed safely to only part of that minute.
    """
    captured = _utc(captured_at_utc or datetime.now(UTC), "captured_at_utc")
    end = _utc(ticker.observed_at_utc or ticker.retrieved_at_utc, "ticker market observation time")
    start = end - ROLLING_WINDOW
    if end.second or end.microsecond:
        raise InvalidWindowError("source kline data is one-minute resolution; sub-minute boundary coverage cannot be proven")
    _validate_identity(ticker, history, start_boundary_history, end_boundary_history)
    _validate_freshness(
        ticker,
        (history, start_boundary_history, end_boundary_history),
        captured,
        max_input_age,
        max_observation_age or max_input_age,
    )
    _price(ticker.price, "ticker price")
    first_full_hour = _ceil_hour(start)
    end_full_hour = _floor_hour(end)
    hourly = _required(history, start=first_full_hour, end=end_full_hour, interval_minutes=HOURLY_INTERVAL_MINUTES, label="hourly")
    start_boundary = _required(start_boundary_history, start=start, end=first_full_hour, interval_minutes=MINUTE_INTERVAL_MINUTES, label="start boundary")
    end_boundary = _required(end_boundary_history, start=end_full_hour, end=end, interval_minutes=MINUTE_INTERVAL_MINUTES, label="end boundary")
    # The current price is the point observation at the inclusive window end,
    # so it participates in the rolling high exactly as in Phase 3.4.
    high = max((ticker.price, *(candle.high for candle in (*hourly, *start_boundary, *end_boundary))))
    resolution = 60 if start_boundary or end_boundary else 3600
    snapshot = MarketSnapshot(schema_version="1.0.0", snapshot_id=_snapshot_id(ticker.source, captured, start, end, ticker.price, high), captured_at_utc=_iso(captured), source_exchange=BYBIT_EXCHANGE, market_type=SPOT_MARKET, symbol=BTCUSDT_SYMBOL, current_price_usdt=ticker.price, rolling_7d_high_usdt=high, window_start_utc=_iso(start), window_end_utc=_iso(end), same_source_price_and_high=True, full_168h_coverage=True, fresh=True, observation_resolution_seconds=resolution, trade_level_exact=False, source=ticker.source, external_symbol=ticker.external_symbol or EXACT_EXTERNAL_SYMBOL)
    validate_artifact("market_snapshot", snapshot.to_dict())
    return snapshot
