"""Read-only Bybit V5 adapter for the canonical BTCUSDT Spot market.

Public endpoints:

* ``GET /v5/market/tickers?category=spot&symbol=BTCUSDT``
* ``GET /v5/market/kline?category=spot&symbol=BTCUSDT&interval=60``

Kline requests also carry explicit ``start``, ``end``, and ``limit`` values.
No authentication headers, API keys, or trading endpoints are used.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from ..errors import (
    DataUnavailableError,
    InsufficientHistoryError,
    InvalidMarketIdentityError,
    InvalidResponseError,
    RateLimitedError,
    SourceMismatchError,
)
from .http import (
    HttpResponse,
    PublicHttpTransport,
    TransportConnectionError,
    TransportTimeout,
)
from .models import Candle, CandleHistory, Ticker


BYBIT_BASE_URL = "https://api.bybit.com"
BYBIT_SOURCE = "bybit_api"
BYBIT_EXCHANGE = "Bybit"
SPOT_MARKET = "spot"
BTCUSDT_SYMBOL = "BTCUSDT"
TICKER_PATH = "/v5/market/tickers"
KLINE_PATH = "/v5/market/kline"
HOURLY_INTERVAL = "60"
HOURLY_INTERVAL_MINUTES = 60
DEFAULT_HISTORY_HOURS = 168


class HttpGetter(Protocol):
    def get(
        self, path: str, params: Mapping[str, str | int]
    ) -> HttpResponse: ...


@dataclass(frozen=True)
class _KlinePage:
    candles: tuple[Candle, ...]
    exchange_response_time_utc: datetime | None


def _require_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be a UTC-aware datetime")
    return value.astimezone(UTC)


def _datetime_to_milliseconds(value: datetime) -> int:
    return int(_require_utc(value, "datetime").timestamp() * 1000)


def _milliseconds_to_datetime(value: Any, label: str) -> datetime:
    if isinstance(value, bool):
        raise InvalidResponseError(f"{label} must be a Unix timestamp in milliseconds")
    if isinstance(value, int):
        milliseconds = value
    elif isinstance(value, str) and value.isdigit():
        milliseconds = int(value)
    else:
        raise InvalidResponseError(f"{label} must be a Unix timestamp in milliseconds")
    if milliseconds <= 0:
        raise InvalidResponseError(f"{label} must be positive")
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidResponseError(f"{label} is outside the supported date range") from exc


def _decimal(
    value: Any,
    label: str,
    *,
    positive: bool = True,
) -> Decimal:
    if isinstance(value, bool):
        raise InvalidResponseError(f"{label} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidResponseError(f"{label} must be numeric") from exc
    if not parsed.is_finite():
        raise InvalidResponseError(f"{label} must be finite")
    valid = parsed > 0 if positive else parsed >= 0
    if not valid:
        qualifier = "greater than zero" if positive else "zero or greater"
        raise InvalidResponseError(f"{label} must be finite and {qualifier}")
    return parsed


class _BybitResponseParser:
    """Pure validation and normalization for Bybit response bodies."""

    @staticmethod
    def decode(response: HttpResponse) -> dict[str, Any]:
        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidResponseError("Bybit response is not valid UTF-8 JSON") from exc
        if not isinstance(decoded, dict):
            raise InvalidResponseError("Bybit response root must be an object")
        ret_code = decoded.get("retCode")
        if isinstance(ret_code, bool) or not isinstance(ret_code, int):
            raise InvalidResponseError("Bybit response retCode must be an integer")
        if ret_code in {429, 10006}:
            raise RateLimitedError(
                f"Bybit logical rate limit: retCode={ret_code}", retryable=True
            )
        if ret_code != 0:
            message = decoded.get("retMsg")
            raise DataUnavailableError(
                f"Bybit logical API error retCode={ret_code}, retMsg={message!r}"
            )
        return decoded

    @staticmethod
    def result(payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise InvalidResponseError("Bybit response result must be an object")
        return result

    @staticmethod
    def validate_identity(result: dict[str, Any]) -> None:
        category = result.get("category")
        if category != SPOT_MARKET:
            raise InvalidMarketIdentityError(
                f"expected Bybit category='spot', received {category!r}"
            )
        symbol = result.get("symbol")
        if symbol is not None and symbol != BTCUSDT_SYMBOL:
            raise SourceMismatchError(
                f"expected Bybit symbol BTCUSDT, received {symbol!r}"
            )

    @staticmethod
    def response_time(payload: dict[str, Any]) -> datetime | None:
        value = payload.get("time")
        if value is None:
            return None
        return _milliseconds_to_datetime(value, "Bybit response time")

    def ticker(self, payload: dict[str, Any], retrieved_at: datetime) -> Ticker:
        result = self.result(payload)
        self.validate_identity(result)
        rows = result.get("list")
        if not isinstance(rows, list) or not rows:
            raise InvalidResponseError("Bybit ticker result list must be non-empty")
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise InvalidResponseError(
                "Bybit ticker query must return exactly one ticker object"
            )
        row = rows[0]
        if row.get("symbol") != BTCUSDT_SYMBOL:
            raise SourceMismatchError(
                f"expected ticker symbol BTCUSDT, received {row.get('symbol')!r}"
            )
        if "markPrice" in row or "indexPrice" in row:
            raise InvalidMarketIdentityError(
                "ticker contains derivative mark/index price fields"
            )
        return Ticker(
            price=_decimal(row.get("lastPrice"), "ticker lastPrice"),
            retrieved_at_utc=retrieved_at,
            exchange_response_time_utc=self.response_time(payload),
            observed_at_utc=None,
            source=BYBIT_SOURCE,
            exchange=BYBIT_EXCHANGE,
            market=SPOT_MARKET,
            symbol=BTCUSDT_SYMBOL,
        )

    def klines(self, payload: dict[str, Any]) -> _KlinePage:
        result = self.result(payload)
        self.validate_identity(result)
        if result.get("symbol") != BTCUSDT_SYMBOL:
            raise SourceMismatchError(
                f"expected kline symbol BTCUSDT, received {result.get('symbol')!r}"
            )
        rows = result.get("list")
        if not isinstance(rows, list):
            raise InvalidResponseError("Bybit kline result list must be an array")
        candles = tuple(self._candle(row, position) for position, row in enumerate(rows))
        return _KlinePage(candles, self.response_time(payload))

    @staticmethod
    def _candle(row: Any, position: int) -> Candle:
        if not isinstance(row, list) or len(row) < 6:
            raise InvalidResponseError(
                f"kline row {position} must contain timestamp, OHLC, and volume"
            )
        open_time = _milliseconds_to_datetime(row[0], f"kline row {position} timestamp")
        if _datetime_to_milliseconds(open_time) % (60 * 60 * 1000) != 0:
            raise InvalidResponseError(
                f"kline row {position} timestamp is not aligned to an hourly candle"
            )
        open_price = _decimal(row[1], f"kline row {position} open")
        high = _decimal(row[2], f"kline row {position} high")
        low = _decimal(row[3], f"kline row {position} low")
        close = _decimal(row[4], f"kline row {position} close")
        volume = _decimal(row[5], f"kline row {position} volume", positive=False)
        if low > high or not all(low <= price <= high for price in (open_price, close)):
            raise InvalidResponseError(f"kline row {position} contains contradictory OHLC")
        return Candle(
            open_time_utc=open_time,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            source=BYBIT_SOURCE,
            exchange=BYBIT_EXCHANGE,
            market=SPOT_MARKET,
            symbol=BTCUSDT_SYMBOL,
        )


class BybitSpotAdapter:
    """Retrieve BTCUSDT Spot ticker and complete hourly candle ranges."""

    def __init__(
        self,
        *,
        transport: HttpGetter | None = None,
        clock: Callable[[], datetime] | None = None,
        page_limit: int = 200,
        max_pages: int = 20,
    ) -> None:
        if not 1 <= page_limit <= 1000:
            raise ValueError("page_limit must be within Bybit's 1..1000 range")
        if not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be within 1..100")
        self._transport = transport or PublicHttpTransport(BYBIT_BASE_URL)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.page_limit = page_limit
        self.max_pages = max_pages
        self._parser = _BybitResponseParser()

    def fetch_ticker(self) -> Ticker:
        payload = self._request_json(
            TICKER_PATH,
            {"category": SPOT_MARKET, "symbol": BTCUSDT_SYMBOL},
        )
        retrieved_at = _require_utc(self._clock(), "retrieval time")
        return self._parser.ticker(payload, retrieved_at)

    def fetch_candles(
        self,
        *,
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
        lookback_hours: int = DEFAULT_HISTORY_HOURS,
    ) -> CandleHistory:
        """Return ordered complete hourly bars overlapping the requested range.

        The method retrieves 168 hours by default but does not calculate a
        rolling high. An interval is complete only when every overlapping hourly
        candle open is present; gaps and bounded-pagination exhaustion fail
        closed with ``INSUFFICIENT_HISTORY``.
        """

        if (
            isinstance(lookback_hours, bool)
            or not isinstance(lookback_hours, int)
            or lookback_hours <= 0
        ):
            raise ValueError("lookback_hours must be a positive integer")
        requested_end = _require_utc(end_utc or self._clock(), "end_utc")
        requested_start = _require_utc(
            start_utc or requested_end - timedelta(hours=lookback_hours),
            "start_utc",
        )
        if requested_start >= requested_end:
            raise ValueError("start_utc must be earlier than end_utc")

        interval_ms = HOURLY_INTERVAL_MINUTES * 60 * 1000
        requested_start_ms = _datetime_to_milliseconds(requested_start)
        requested_end_ms = _datetime_to_milliseconds(requested_end)
        first_required_ms = requested_start_ms - (requested_start_ms % interval_ms)
        last_required_ms = (requested_end_ms - 1) - ((requested_end_ms - 1) % interval_ms)

        by_timestamp: dict[int, Candle] = {}
        duplicate_count = 0
        request_count = 0
        cursor_end_ms = requested_end_ms
        seen_oldest: set[int] = set()

        for _ in range(self.max_pages):
            request_count += 1
            payload = self._request_json(
                KLINE_PATH,
                {
                    "category": SPOT_MARKET,
                    "symbol": BTCUSDT_SYMBOL,
                    "interval": HOURLY_INTERVAL,
                    "start": first_required_ms,
                    "end": cursor_end_ms,
                    "limit": self.page_limit,
                },
            )
            page = self._parser.klines(payload)
            if not page.candles:
                break
            page_timestamps = [
                _datetime_to_milliseconds(candle.open_time_utc)
                for candle in page.candles
            ]
            for timestamp, candle in zip(page_timestamps, page.candles, strict=True):
                if first_required_ms <= timestamp <= last_required_ms:
                    if timestamp in by_timestamp:
                        duplicate_count += 1
                    by_timestamp[timestamp] = candle
            oldest = min(page_timestamps)
            if oldest <= first_required_ms:
                break
            if oldest in seen_oldest:
                break
            seen_oldest.add(oldest)
            next_cursor = oldest - 1
            if next_cursor >= cursor_end_ms:
                raise InvalidResponseError("Bybit kline pagination made no progress")
            cursor_end_ms = next_cursor

        expected = tuple(range(first_required_ms, last_required_ms + 1, interval_ms))
        missing = [timestamp for timestamp in expected if timestamp not in by_timestamp]
        if missing:
            first_missing = _milliseconds_to_datetime(
                missing[0], "missing candle timestamp"
            ).isoformat()
            raise InsufficientHistoryError(
                "Bybit hourly history is incomplete: "
                f"missing {len(missing)} of {len(expected)} candles; "
                f"first missing open={first_missing}; requests={request_count}"
            )

        ordered = tuple(by_timestamp[timestamp] for timestamp in expected)
        retrieved_at = _require_utc(self._clock(), "retrieval time")
        return CandleHistory(
            candles=ordered,
            requested_start_utc=requested_start,
            requested_end_utc=requested_end,
            coverage_start_utc=ordered[0].open_time_utc,
            coverage_end_utc=min(
                requested_end,
                ordered[-1].open_time_utc
                + timedelta(minutes=HOURLY_INTERVAL_MINUTES),
            ),
            retrieved_at_utc=retrieved_at,
            interval_minutes=HOURLY_INTERVAL_MINUTES,
            source=BYBIT_SOURCE,
            exchange=BYBIT_EXCHANGE,
            market=SPOT_MARKET,
            symbol=BTCUSDT_SYMBOL,
            request_count=request_count,
            duplicate_count=duplicate_count,
            complete=True,
        )

    def _request_json(
        self, path: str, params: Mapping[str, str | int]
    ) -> dict[str, Any]:
        try:
            response = self._transport.get(path, params)
        except TransportTimeout as exc:
            raise DataUnavailableError(
                f"Bybit request timed out after {exc.attempts} attempts",
                retryable=True,
            ) from exc
        except TransportConnectionError as exc:
            raise DataUnavailableError(
                f"Bybit connection failed after {exc.attempts} attempts",
                retryable=True,
            ) from exc

        if response.status == 403:
            raise DataUnavailableError(
                "Bybit returned HTTP 403 (forbidden, IP restriction, or IP rate limit)",
                status_code=403,
            )
        if response.status == 429:
            raise RateLimitedError(
                "Bybit returned HTTP 429 after bounded retry",
                status_code=429,
                retryable=True,
            )
        if 500 <= response.status <= 599:
            raise DataUnavailableError(
                f"Bybit returned transient HTTP {response.status} after bounded retry",
                status_code=response.status,
                retryable=True,
            )
        if response.status != 200:
            raise InvalidResponseError(
                f"Bybit returned unexpected HTTP {response.status}",
                status_code=response.status,
            )
        return self._parser.decode(response)
