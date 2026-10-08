"""Approved public BTC spot fallback sources for V1 recommendation runs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Protocol, Any

from ..errors import (
    AllSourcesUnavailableError,
    ContradictoryPriceDataError,
    DataStaleError,
    DataUnavailableError,
    InsufficientHistoryError,
    InvalidMarketIdentityError,
    InvalidResponseError,
    InvalidWindowError,
    RateLimitedError,
    SourceMismatchError,
)
from ..models import MarketSnapshot
from ..schemas import validate_artifact
from .http import HttpResponse, PublicHttpTransport, TransportConnectionError, TransportTimeout
from .models import Candle, CandleHistory, Ticker
from .provider import BybitSnapshotSource, FALLBACK_FAILURES, HARD_FAILURES

SPOT_MARKET = "spot"
CANONICAL_SYMBOL = "BTCUSDT"
ROLLING_WINDOW = timedelta(hours=168)
MAX_CLOCK_SKEW = timedelta(seconds=5)

BINANCE_SOURCE = "binance_api"
BINANCE_EXCHANGE = "Binance"
BINANCE_EXTERNAL_SYMBOL = "BINANCE:BTCUSDT"
BINANCE_BASE_URL = "https://data-api.binance.vision"

KUCOIN_SOURCE = "kucoin_api"
KUCOIN_EXCHANGE = "KuCoin"
KUCOIN_EXTERNAL_SYMBOL = "KUCOIN:BTC-USDT"
KUCOIN_BASE_URL = "https://api.kucoin.com"


class HttpGetter(Protocol):
    def get(self, path: str, params: Mapping[str, str | int]) -> HttpResponse: ...


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise InvalidResponseError(f"{label} must be UTC-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _decimal(value: Any, label: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise InvalidResponseError(f"{label} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidResponseError(f"{label} must be numeric") from exc
    if not parsed.is_finite() or (parsed <= 0 if positive else parsed < 0):
        raise InvalidResponseError(f"{label} is invalid")
    return parsed


def _ms(value: datetime) -> int:
    return int(_utc(value, "datetime").timestamp() * 1000)


def _sec(value: datetime) -> int:
    return int(_utc(value, "datetime").timestamp())


class _BaseSpotAdapter:
    source: str
    exchange: str
    external_symbol: str

    def __init__(self, transport: HttpGetter, *, clock: Callable[[], datetime]) -> None:
        self._transport = transport
        self._clock = clock

    def _request(self, path: str, params: Mapping[str, str | int]) -> HttpResponse:
        try:
            response = self._transport.get(path, params)
        except (TransportTimeout, TransportConnectionError) as exc:
            raise DataUnavailableError(
                f"{self.exchange} public market-data request failed", retryable=True
            ) from exc
        if response.status == 429:
            raise RateLimitedError(f"{self.exchange} rate limited", retryable=True)
        if not 200 <= response.status < 300:
            raise DataUnavailableError(
                f"{self.exchange} public market-data HTTP {response.status}",
                retryable=response.status >= 500,
            )
        return response

    def _history(
        self,
        rows: list[Any],
        *,
        start_utc: datetime,
        end_utc: datetime,
        interval_minutes: int,
        row_parser: Callable[[Any, int], Candle],
    ) -> CandleHistory:
        interval = timedelta(minutes=interval_minutes)
        expected = []
        cursor = start_utc
        while cursor < end_utc:
            expected.append(cursor)
            cursor += interval
        parsed = [row_parser(row, interval_minutes) for row in rows]
        by_open: dict[datetime, Candle] = {}
        duplicates = 0
        for candle in parsed:
            if candle.open_time_utc in by_open:
                duplicates += 1
            by_open[candle.open_time_utc] = candle
        missing = [ts for ts in expected if ts not in by_open]
        if missing:
            raise InsufficientHistoryError(
                f"{self.exchange} {interval_minutes}-minute Spot history is incomplete; "
                f"missing {len(missing)} candles"
            )
        ordered = tuple(by_open[ts] for ts in expected)
        retrieved = _utc(self._clock(), "retrieval time")
        return CandleHistory(
            candles=ordered,
            requested_start_utc=start_utc,
            requested_end_utc=end_utc,
            coverage_start_utc=start_utc,
            coverage_end_utc=end_utc,
            retrieved_at_utc=retrieved,
            interval_minutes=interval_minutes,
            source=self.source,
            exchange=self.exchange,
            market=SPOT_MARKET,
            symbol=CANONICAL_SYMBOL,
            request_count=1,
            duplicate_count=duplicates,
            complete=True,
            external_symbol=self.external_symbol,
        )


class BinanceSpotAdapter(_BaseSpotAdapter):
    source = BINANCE_SOURCE
    exchange = BINANCE_EXCHANGE
    external_symbol = BINANCE_EXTERNAL_SYMBOL

    def fetch_ticker(self) -> Ticker:
        response = self._request("/api/v3/ticker/price", {"symbol": "BTCUSDT"})
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except Exception as exc:
            raise InvalidResponseError("Binance ticker is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("symbol") != "BTCUSDT":
            raise SourceMismatchError("Binance ticker identity mismatch")
        retrieved = _utc(self._clock(), "retrieval time")
        return Ticker(
            price=_decimal(payload.get("price"), "Binance ticker price"),
            retrieved_at_utc=retrieved,
            exchange_response_time_utc=None,
            observed_at_utc=None,
            source=self.source,
            exchange=self.exchange,
            market=SPOT_MARKET,
            symbol=CANONICAL_SYMBOL,
            external_symbol=self.external_symbol,
        )

    def fetch_candles(self, *, start_utc: datetime, end_utc: datetime, interval_minutes: int) -> CandleHistory:
        interval = "1m" if interval_minutes == 1 else "1h" if interval_minutes == 60 else None
        if interval is None:
            raise ValueError("interval_minutes must be 1 or 60")
        expected_count = int((end_utc - start_utc).total_seconds() // (interval_minutes * 60))
        if expected_count > 1000:
            raise ValueError("Binance request exceeds 1000-candle public limit")
        response = self._request(
            "/api/v3/klines",
            {
                "symbol": "BTCUSDT",
                "interval": interval,
                "startTime": _ms(start_utc),
                "endTime": _ms(end_utc) - 1,
                "limit": max(expected_count, 1),
            },
        )
        try:
            rows = json.loads(response.body.decode("utf-8"))
        except Exception as exc:
            raise InvalidResponseError("Binance kline response is not valid JSON") from exc
        if not isinstance(rows, list):
            raise InvalidResponseError("Binance kline response root must be an array")

        def parse(row: Any, minutes: int) -> Candle:
            if not isinstance(row, list) or len(row) < 6:
                raise InvalidResponseError("Binance kline row is malformed")
            opened = datetime.fromtimestamp(int(row[0]) / 1000, UTC)
            candle = Candle(
                open_time_utc=opened,
                open=_decimal(row[1], "open"),
                high=_decimal(row[2], "high"),
                low=_decimal(row[3], "low"),
                close=_decimal(row[4], "close"),
                volume=_decimal(row[5], "volume", positive=False),
                source=self.source,
                exchange=self.exchange,
                market=SPOT_MARKET,
                symbol=CANONICAL_SYMBOL,
                interval_minutes=minutes,
                external_symbol=self.external_symbol,
            )
            if candle.low > candle.high or not all(candle.low <= p <= candle.high for p in (candle.open, candle.close)):
                raise ContradictoryPriceDataError("Binance candle contains contradictory OHLC")
            return candle

        return self._history(
            rows,
            start_utc=start_utc,
            end_utc=end_utc,
            interval_minutes=interval_minutes,
            row_parser=parse,
        )


class KuCoinSpotAdapter(_BaseSpotAdapter):
    source = KUCOIN_SOURCE
    exchange = KUCOIN_EXCHANGE
    external_symbol = KUCOIN_EXTERNAL_SYMBOL

    def fetch_ticker(self) -> Ticker:
        response = self._request(
            "/api/ua/v1/market/ticker",
            {"tradeType": "SPOT", "symbol": "BTC-USDT"},
        )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except Exception as exc:
            raise InvalidResponseError("KuCoin ticker is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("code") != "200000":
            raise DataUnavailableError("KuCoin ticker returned logical API failure")
        data = payload.get("data")
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise InvalidResponseError("KuCoin ticker must contain exactly one row")
        row = rows[0]
        if row.get("symbol") != "BTC-USDT":
            raise SourceMismatchError("KuCoin ticker identity mismatch")
        retrieved = _utc(self._clock(), "retrieval time")
        return Ticker(
            price=_decimal(row.get("lastPrice"), "KuCoin lastPrice"),
            retrieved_at_utc=retrieved,
            exchange_response_time_utc=None,
            observed_at_utc=None,
            source=self.source,
            exchange=self.exchange,
            market=SPOT_MARKET,
            symbol=CANONICAL_SYMBOL,
            external_symbol=self.external_symbol,
        )

    def fetch_candles(self, *, start_utc: datetime, end_utc: datetime, interval_minutes: int) -> CandleHistory:
        kline_type = "1min" if interval_minutes == 1 else "1hour" if interval_minutes == 60 else None
        if kline_type is None:
            raise ValueError("interval_minutes must be 1 or 60")
        response = self._request(
            "/api/v1/market/candles",
            {
                "symbol": "BTC-USDT",
                "type": kline_type,
                "startAt": _sec(start_utc),
                "endAt": _sec(end_utc),
            },
        )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except Exception as exc:
            raise InvalidResponseError("KuCoin kline response is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("code") != "200000":
            raise DataUnavailableError("KuCoin kline returned logical API failure")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise InvalidResponseError("KuCoin kline data must be an array")

        def parse(row: Any, minutes: int) -> Candle:
            if not isinstance(row, list) or len(row) < 7:
                raise InvalidResponseError("KuCoin kline row is malformed")
            opened = datetime.fromtimestamp(int(row[0]), UTC)
            candle = Candle(
                open_time_utc=opened,
                open=_decimal(row[1], "open"),
                close=_decimal(row[2], "close"),
                high=_decimal(row[3], "high"),
                low=_decimal(row[4], "low"),
                volume=_decimal(row[5], "volume", positive=False),
                source=self.source,
                exchange=self.exchange,
                market=SPOT_MARKET,
                symbol=CANONICAL_SYMBOL,
                interval_minutes=minutes,
                external_symbol=self.external_symbol,
            )
            if candle.low > candle.high or not all(candle.low <= p <= candle.high for p in (candle.open, candle.close)):
                raise ContradictoryPriceDataError("KuCoin candle contains contradictory OHLC")
            return candle

        return self._history(
            rows,
            start_utc=start_utc,
            end_utc=end_utc,
            interval_minutes=interval_minutes,
            row_parser=parse,
        )


class ApprovedSpotSnapshotSource:
    def __init__(self, adapter: _BaseSpotAdapter, *, clock: Callable[[], datetime], max_input_age: timedelta) -> None:
        self.adapter = adapter
        self.source = adapter.source
        self._clock = clock
        self.max_input_age = max_input_age

    def get_market_snapshot(self, *, captured_at_utc: datetime | None = None) -> MarketSnapshot:
        ticker = self.adapter.fetch_ticker()
        end = ticker.observed_at_utc or ticker.retrieved_at_utc
        if end.second or end.microsecond:
            raise InvalidWindowError("Spot fallback source cannot prove sub-minute boundary")
        start = end - ROLLING_WINDOW
        first_full_hour = start.replace(minute=0, second=0, microsecond=0)
        if first_full_hour < start:
            first_full_hour += timedelta(hours=1)
        end_full_hour = end.replace(minute=0, second=0, microsecond=0)

        hourly = self.adapter.fetch_candles(
            start_utc=first_full_hour, end_utc=end_full_hour, interval_minutes=60
        )
        start_boundary = None
        if start < first_full_hour:
            start_boundary = self.adapter.fetch_candles(
                start_utc=start, end_utc=first_full_hour, interval_minutes=1
            )
        end_boundary = None
        if end_full_hour < end:
            end_boundary = self.adapter.fetch_candles(
                start_utc=end_full_hour, end_utc=end, interval_minutes=1
            )
        return _build_snapshot(
            ticker,
            hourly,
            start_boundary,
            end_boundary,
            captured_at_utc or self._clock(),
            self.max_input_age,
        )


def _build_snapshot(
    ticker: Ticker,
    hourly: CandleHistory,
    start_boundary: CandleHistory | None,
    end_boundary: CandleHistory | None,
    captured_at: datetime,
    max_age: timedelta,
) -> MarketSnapshot:
    captured = _utc(captured_at, "captured_at")
    end = ticker.observed_at_utc or ticker.retrieved_at_utc
    start = end - ROLLING_WINDOW
    values = [hourly]
    if start_boundary is not None:
        values.append(start_boundary)
    if end_boundary is not None:
        values.append(end_boundary)

    for history in values:
        if (
            history.source != ticker.source
            or history.exchange != ticker.exchange
            or history.market != SPOT_MARKET
            or history.symbol != CANONICAL_SYMBOL
            or history.external_symbol != ticker.external_symbol
        ):
            raise SourceMismatchError("fallback snapshot mixed source or market identity")
        if captured - history.retrieved_at_utc > max_age:
            raise DataStaleError("fallback candle history is stale")
    if captured - ticker.retrieved_at_utc > max_age:
        raise DataStaleError("fallback ticker is stale")
    if ticker.retrieved_at_utc > captured + MAX_CLOCK_SKEW:
        raise InvalidResponseError("fallback ticker retrieval time is in the future")

    candles = tuple(hourly.candles) + tuple(start_boundary.candles if start_boundary else ()) + tuple(end_boundary.candles if end_boundary else ())
    high = max([ticker.price, *(c.high for c in candles)])
    material = "|".join(
        (
            ticker.source,
            ticker.exchange,
            ticker.external_symbol,
            _iso(captured),
            _iso(start),
            _iso(end),
            str(ticker.price),
            str(high),
        )
    )
    snapshot = MarketSnapshot(
        schema_version="1.0.0",
        snapshot_id=f"market_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}",
        captured_at_utc=_iso(captured),
        source_exchange=ticker.exchange,
        market_type=SPOT_MARKET,
        symbol=CANONICAL_SYMBOL,
        current_price_usdt=ticker.price,
        rolling_7d_high_usdt=high,
        window_start_utc=_iso(start),
        window_end_utc=_iso(end),
        same_source_price_and_high=True,
        full_168h_coverage=True,
        fresh=True,
        observation_resolution_seconds=60 if (start_boundary or end_boundary) else 3600,
        trade_level_exact=False,
        source=ticker.source,
        external_symbol=ticker.external_symbol,
        primary_source="bybit_api",
    )
    validate_artifact("market_snapshot", snapshot.to_dict())
    return snapshot


class OrderedApprovedSpotProvider:
    """Bybit Spot -> Binance Spot -> KuCoin Spot, with complete-source atomicity."""

    def __init__(self, sources: tuple[Any, ...]) -> None:
        expected = ("bybit_api", BINANCE_SOURCE, KUCOIN_SOURCE)
        actual = tuple(source.source for source in sources)
        if actual != expected:
            raise ValueError(f"approved source order must be {expected!r}")
        self.sources = sources

    def get_market_snapshot(self, *, captured_at_utc: datetime | None = None) -> MarketSnapshot:
        failures = []
        for index, source in enumerate(self.sources):
            try:
                snapshot = source.get_market_snapshot(captured_at_utc=captured_at_utc)
            except HARD_FAILURES:
                raise
            except FALLBACK_FAILURES as exc:
                failures.append(exc)
                continue
            if index == 0:
                return snapshot
            primary_failure = failures[0] if failures else None
            updated = MarketSnapshot(
                **{
                    **snapshot.__dict__,
                    "primary_source": "bybit_api",
                    "primary_failure_category": None if primary_failure is None else primary_failure.code.value,
                    "fallback_attempted": True,
                }
            )
            validate_artifact("market_snapshot", updated.to_dict())
            return updated
        if failures:
            raise AllSourcesUnavailableError(
                "Bybit, Binance, and KuCoin approved BTC Spot sources are unavailable",
                primary_source="bybit_api",
                primary_failure=failures[0],
                fallback_source=KUCOIN_SOURCE,
                fallback_failure=failures[-1],
            )
        raise DataUnavailableError("no approved BTC Spot source configured")


def build_binance_source(*, clock: Callable[[], datetime], max_input_age: timedelta) -> ApprovedSpotSnapshotSource:
    return ApprovedSpotSnapshotSource(
        BinanceSpotAdapter(PublicHttpTransport(BINANCE_BASE_URL), clock=clock),
        clock=clock,
        max_input_age=max_input_age,
    )


def build_kucoin_source(*, clock: Callable[[], datetime], max_input_age: timedelta) -> ApprovedSpotSnapshotSource:
    return ApprovedSpotSnapshotSource(
        KuCoinSpotAdapter(PublicHttpTransport(KUCOIN_BASE_URL), clock=clock),
        clock=clock,
        max_input_age=max_input_age,
    )
