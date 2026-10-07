"""Exact ``BYBIT:BTCUSDT`` TradingView chart adapter.

The wire protocol is isolated behind ``TradingViewChartTransport``.  The
adapter accepts only resolved metadata for the exact Bybit Spot symbol and
normalizes completed hourly bars into the Phase 3.4 snapshot inputs.
"""

from __future__ import annotations

import json
import secrets
import string
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from ..errors import (
    ContradictoryPriceDataError,
    DataUnavailableError,
    InsufficientHistoryError,
    InvalidMarketIdentityError,
    InvalidResponseError,
    InvalidWindowError,
    RateLimitedError,
    SourceMismatchError,
)
from .bybit import BTCUSDT_SYMBOL, BYBIT_EXCHANGE, SPOT_MARKET
from .models import Candle, CandleHistory, Ticker


TRADINGVIEW_SOURCE = "tradingview"
TRADINGVIEW_EXTERNAL_SYMBOL = "BYBIT:BTCUSDT"
TRADINGVIEW_WEBSOCKET_URL = "wss://data.tradingview.com/socket.io/websocket"
TRADINGVIEW_ORIGIN = "https://www.tradingview.com"
TRADINGVIEW_INTERVAL_MINUTES = 60
DEFAULT_BAR_COUNT = 180


class TradingViewChartTransport(Protocol):
    def fetch_chart(
        self, *, external_symbol: str, interval_minutes: int, bar_count: int
    ) -> Mapping[str, Any]: ...


def _frame(method: str, params: Sequence[Any]) -> str:
    payload = json.dumps({"m": method, "p": list(params)}, separators=(",", ":"))
    return f"~m~{len(payload.encode('utf-8'))}~m~{payload}"


def _messages(packet: str) -> tuple[dict[str, Any], ...]:
    """Decode one or more TradingView length-framed JSON messages."""

    decoded: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(packet):
        marker = packet.find("~m~", cursor)
        if marker < 0:
            break
        length_end = packet.find("~m~", marker + 3)
        if length_end < 0:
            raise InvalidResponseError("TradingView protocol frame is truncated")
        length_text = packet[marker + 3 : length_end]
        if not length_text.isdigit():
            raise InvalidResponseError("TradingView protocol frame length is invalid")
        payload_start = length_end + 3
        payload_end = payload_start + int(length_text)
        payload = packet[payload_start:payload_end]
        if len(payload.encode("utf-8")) != int(length_text):
            raise InvalidResponseError("TradingView protocol frame length mismatches payload")
        cursor = payload_end
        if payload.startswith("~h~"):
            continue
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise InvalidResponseError("TradingView protocol payload is not JSON") from exc
        if not isinstance(value, dict):
            raise InvalidResponseError("TradingView protocol payload must be an object")
        decoded.append(value)
    return tuple(decoded)


class WebSocketTradingViewTransport:
    """Bounded read-only client for TradingView's chart websocket protocol."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _session(prefix: str) -> str:
        alphabet = string.ascii_lowercase
        return prefix + "_" + "".join(secrets.choice(alphabet) for _ in range(12))

    def fetch_chart(
        self, *, external_symbol: str, interval_minutes: int, bar_count: int
    ) -> Mapping[str, Any]:
        if external_symbol != TRADINGVIEW_EXTERNAL_SYMBOL:
            raise InvalidMarketIdentityError(
                "TradingView transport is pinned to exact BYBIT:BTCUSDT"
            )
        if interval_minutes != TRADINGVIEW_INTERVAL_MINUTES:
            raise InvalidWindowError("TradingView fallback supports only completed hourly bars")
        if not 169 <= bar_count <= 500:
            raise ValueError("bar_count must be within 169..500")
        try:
            import websocket  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise DataUnavailableError(
                "websocket-client dependency is unavailable", source=TRADINGVIEW_SOURCE
            ) from exc

        chart_session = self._session("cs")
        connection = None
        resolved: Mapping[str, Any] | None = None
        bars: list[Any] = []
        try:
            connection = websocket.create_connection(
                TRADINGVIEW_WEBSOCKET_URL,
                timeout=self.timeout_seconds,
                origin=TRADINGVIEW_ORIGIN,
            )
            commands = (
                ("set_auth_token", ["unauthorized_user_token"]),
                ("chart_create_session", [chart_session, ""]),
                (
                    "resolve_symbol",
                    [
                        chart_session,
                        "symbol_1",
                        "="
                        + json.dumps(
                            {
                                "symbol": external_symbol,
                                "adjustment": "splits",
                                "session": "regular",
                            },
                            separators=(",", ":"),
                        ),
                    ],
                ),
                (
                    "create_series",
                    [
                        chart_session,
                        "series_1",
                        "series_1",
                        "symbol_1",
                        str(interval_minutes),
                        bar_count,
                    ],
                ),
            )
            for method, params in commands:
                connection.send(_frame(method, params))

            completed = False
            while not completed:
                packet = connection.recv()
                if not isinstance(packet, str):
                    raise InvalidResponseError("TradingView returned a non-text frame")
                if "~h~" in packet and not _messages(packet):
                    connection.send(packet)
                    continue
                for message in _messages(packet):
                    method = message.get("m")
                    params = message.get("p")
                    if method in {"critical_error", "protocol_error", "series_error"}:
                        detail = str(params)
                        if "rate" in detail.lower() or "limit" in detail.lower():
                            raise RateLimitedError(
                                "TradingView chart protocol rate limited the request",
                                source=TRADINGVIEW_SOURCE,
                            )
                        raise DataUnavailableError(
                            f"TradingView chart protocol failed: {method}",
                            source=TRADINGVIEW_SOURCE,
                        )
                    if method == "symbol_resolved" and isinstance(params, list) and len(params) >= 3:
                        if isinstance(params[2], dict):
                            resolved = params[2]
                    if method == "timescale_update" and isinstance(params, list) and len(params) >= 2 and isinstance(params[1], dict):
                        series = params[1].get("series_1")
                        if isinstance(series, dict) and isinstance(series.get("s"), list):
                            bars.extend(series["s"])
                    if method == "series_completed" and isinstance(params, list) and "series_1" in params:
                        completed = True
            return {"resolved_symbol": resolved, "bars": bars}
        except (RateLimitedError, DataUnavailableError, InvalidResponseError):
            raise
        except Exception as exc:
            # websocket-client uses several transport exception subclasses; none
            # of their raw messages or payloads are exposed beyond this boundary.
            raise DataUnavailableError(
                "TradingView websocket connection or read failed",
                retryable=True,
                source=TRADINGVIEW_SOURCE,
            ) from exc
        finally:
            if connection is not None:
                connection.close()


def _decimal(value: Any, label: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise InvalidResponseError(f"{label} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidResponseError(f"{label} must be numeric") from exc
    valid = parsed.is_finite() and (parsed > 0 if positive else parsed >= 0)
    if not valid:
        raise InvalidResponseError(f"{label} is outside the accepted numeric range")
    return parsed


class TradingViewBybitSpotAdapter:
    """Normalize a complete exact-symbol TradingView response atomically."""

    def __init__(
        self,
        *,
        transport: TradingViewChartTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        bar_count: int = DEFAULT_BAR_COUNT,
    ) -> None:
        if not 169 <= bar_count <= 500:
            raise ValueError("bar_count must be within 169..500")
        self._transport = transport or WebSocketTradingViewTransport()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.bar_count = bar_count

    @staticmethod
    def validate_identity(metadata: Mapping[str, Any]) -> None:
        resolved = metadata.get("pro_name")
        exchange = metadata.get("exchange")
        listed_exchange = metadata.get("listed_exchange")
        if resolved != TRADINGVIEW_EXTERNAL_SYMBOL:
            if isinstance(resolved, str) and resolved.endswith(".P"):
                raise InvalidMarketIdentityError(
                    "TradingView perpetual identity is forbidden"
                )
            raise SourceMismatchError(
                f"expected TradingView symbol {TRADINGVIEW_EXTERNAL_SYMBOL}, received {resolved!r}"
            )
        if exchange != BYBIT_EXCHANGE or listed_exchange != "BYBIT":
            raise InvalidMarketIdentityError(
                "TradingView metadata does not prove the Bybit exchange"
            )
        if metadata.get("type") != SPOT_MARKET:
            raise InvalidMarketIdentityError(
                "TradingView metadata does not prove the Spot market"
            )
        identity_text = " ".join(
            str(metadata.get(key, ""))
            for key in ("pro_name", "description", "type", "typespecs")
        ).lower()
        if any(word in identity_text for word in ("perpetual", "future", "swap")):
            raise InvalidMarketIdentityError(
                "TradingView metadata contains derivative identity"
            )

    def fetch_inputs(self) -> tuple[Ticker, CandleHistory]:
        response = self._transport.fetch_chart(
            external_symbol=TRADINGVIEW_EXTERNAL_SYMBOL,
            interval_minutes=TRADINGVIEW_INTERVAL_MINUTES,
            bar_count=self.bar_count,
        )
        metadata = response.get("resolved_symbol")
        if not isinstance(metadata, Mapping):
            raise InvalidResponseError("TradingView omitted resolved symbol metadata")
        self.validate_identity(metadata)
        retrieved = self._clock()
        if retrieved.tzinfo is None or retrieved.utcoffset() != timedelta(0):
            raise InvalidResponseError("TradingView retrieval clock must be UTC-aware")
        retrieved = retrieved.astimezone(UTC)
        raw_bars = response.get("bars")
        if not isinstance(raw_bars, list):
            raise InvalidResponseError("TradingView bars must be an array")

        by_open: dict[datetime, Candle] = {}
        duplicates = 0
        for position, raw in enumerate(raw_bars):
            if not isinstance(raw, Mapping) or not isinstance(raw.get("v"), list):
                raise InvalidResponseError(f"TradingView bar {position} is malformed")
            values = raw["v"]
            if len(values) < 6:
                raise InvalidResponseError(f"TradingView bar {position} lacks OHLCV")
            timestamp, open_value, high_value, low_value, close_value, volume_value = values[:6]
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                raise InvalidResponseError(f"TradingView bar {position} timestamp is invalid")
            try:
                opened = datetime.fromtimestamp(timestamp, tz=UTC)
            except (OverflowError, OSError, ValueError) as exc:
                raise InvalidResponseError(f"TradingView bar {position} timestamp is invalid") from exc
            if opened.minute or opened.second or opened.microsecond:
                raise InvalidWindowError("TradingView hourly bar is not hour-aligned")
            open_price = _decimal(open_value, f"TradingView bar {position} open")
            high = _decimal(high_value, f"TradingView bar {position} high")
            low = _decimal(low_value, f"TradingView bar {position} low")
            close = _decimal(close_value, f"TradingView bar {position} close")
            volume = _decimal(volume_value, f"TradingView bar {position} volume", positive=False)
            if low > high or not all(low <= price <= high for price in (open_price, close)):
                raise ContradictoryPriceDataError(
                    f"TradingView bar {position} contains contradictory OHLC"
                )
            if opened + timedelta(hours=1) > retrieved:
                continue
            if opened in by_open:
                duplicates += 1
            by_open[opened] = Candle(
                open_time_utc=opened,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                source=TRADINGVIEW_SOURCE,
                exchange=BYBIT_EXCHANGE,
                market=SPOT_MARKET,
                symbol=BTCUSDT_SYMBOL,
                interval_minutes=TRADINGVIEW_INTERVAL_MINUTES,
                external_symbol=TRADINGVIEW_EXTERNAL_SYMBOL,
            )

        ordered = tuple(by_open[key] for key in sorted(by_open))
        if len(ordered) < 168:
            raise InsufficientHistoryError(
                f"TradingView returned only {len(ordered)} completed hourly bars"
            )
        selected = ordered[-168:]
        expected = tuple(
            selected[0].open_time_utc + timedelta(hours=index) for index in range(168)
        )
        if tuple(bar.open_time_utc for bar in selected) != expected:
            raise InsufficientHistoryError("TradingView hourly history contains a gap")
        observed = selected[-1].close_time_exclusive_utc
        ticker = Ticker(
            price=selected[-1].close,
            retrieved_at_utc=retrieved,
            exchange_response_time_utc=None,
            observed_at_utc=observed,
            source=TRADINGVIEW_SOURCE,
            exchange=BYBIT_EXCHANGE,
            market=SPOT_MARKET,
            symbol=BTCUSDT_SYMBOL,
            external_symbol=TRADINGVIEW_EXTERNAL_SYMBOL,
        )
        history = CandleHistory(
            candles=selected,
            requested_start_utc=selected[0].open_time_utc,
            requested_end_utc=observed,
            coverage_start_utc=selected[0].open_time_utc,
            coverage_end_utc=observed,
            retrieved_at_utc=retrieved,
            interval_minutes=TRADINGVIEW_INTERVAL_MINUTES,
            source=TRADINGVIEW_SOURCE,
            exchange=BYBIT_EXCHANGE,
            market=SPOT_MARKET,
            symbol=BTCUSDT_SYMBOL,
            request_count=1,
            duplicate_count=duplicates,
            complete=True,
            external_symbol=TRADINGVIEW_EXTERNAL_SYMBOL,
        )
        return ticker, history
