"""Normalized immutable values returned by read-only market-data adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True)
class MarketIdentity:
    exchange: str
    market: str
    symbol: str


@dataclass(frozen=True)
class Ticker:
    """A normalized last-traded price from one explicit market source.

    ``observed_at_utc`` is deliberately optional. Bybit V5 returns an envelope
    response time but no reliable last-trade observation time on the ticker row.
    """

    price: Decimal
    retrieved_at_utc: datetime
    exchange_response_time_utc: datetime | None
    observed_at_utc: datetime | None
    source: str
    exchange: str
    market: str
    symbol: str

    @property
    def identity(self) -> MarketIdentity:
        return MarketIdentity(self.exchange, self.market, self.symbol)

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": _decimal_text(self.price),
            "retrieved_at_utc": _iso_utc(self.retrieved_at_utc),
            "exchange_response_time_utc": _iso_utc(
                self.exchange_response_time_utc
            ),
            "observed_at_utc": _iso_utc(self.observed_at_utc),
            "source": self.source,
            "exchange": self.exchange,
            "market": self.market,
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class Candle:
    """A Bybit candle covering ``[open_time_utc, close_time_exclusive_utc)``.

    Bybit identifies a kline by its start time.  Making the half-open interval
    explicit prevents a candle high from being used when any part of the candle
    falls outside a requested rolling window.
    """

    open_time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    source: str
    exchange: str
    market: str
    symbol: str
    interval_minutes: int = 60

    @property
    def close_time_exclusive_utc(self) -> datetime:
        return self.open_time_utc + timedelta(minutes=self.interval_minutes)

    @property
    def identity(self) -> MarketIdentity:
        return MarketIdentity(self.exchange, self.market, self.symbol)

    def to_dict(self) -> dict[str, Any]:
        return {
            "open_time_utc": _iso_utc(self.open_time_utc),
            "open": _decimal_text(self.open),
            "high": _decimal_text(self.high),
            "low": _decimal_text(self.low),
            "close": _decimal_text(self.close),
            "volume": _decimal_text(self.volume),
            "source": self.source,
            "exchange": self.exchange,
            "market": self.market,
            "symbol": self.symbol,
            "interval_minutes": self.interval_minutes,
            "close_time_exclusive_utc": _iso_utc(self.close_time_exclusive_utc),
        }


@dataclass(frozen=True)
class CandleHistory:
    """Ordered, deduplicated candles plus proof of requested coverage."""

    candles: tuple[Candle, ...]
    requested_start_utc: datetime
    requested_end_utc: datetime
    coverage_start_utc: datetime
    coverage_end_utc: datetime
    retrieved_at_utc: datetime
    interval_minutes: int
    source: str
    exchange: str
    market: str
    symbol: str
    request_count: int
    duplicate_count: int
    complete: bool = True

    @property
    def identity(self) -> MarketIdentity:
        return MarketIdentity(self.exchange, self.market, self.symbol)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candles": [candle.to_dict() for candle in self.candles],
            "requested_start_utc": _iso_utc(self.requested_start_utc),
            "requested_end_utc": _iso_utc(self.requested_end_utc),
            "coverage_start_utc": _iso_utc(self.coverage_start_utc),
            "coverage_end_utc": _iso_utc(self.coverage_end_utc),
            "retrieved_at_utc": _iso_utc(self.retrieved_at_utc),
            "interval_minutes": self.interval_minutes,
            "source": self.source,
            "exchange": self.exchange,
            "market": self.market,
            "symbol": self.symbol,
            "request_count": self.request_count,
            "duplicate_count": self.duplicate_count,
            "complete": self.complete,
        }
