"""Read-only market-data adapters and normalized domain values."""

from .bybit import BYBIT_SOURCE, BybitSpotAdapter
from .models import Candle, CandleHistory, MarketIdentity, Ticker
from .provider import (
    BybitSnapshotSource,
    FallbackMarketDataProvider,
    TradingViewSnapshotSource,
)
from .snapshot import build_market_snapshot
from .tradingview import (
    TRADINGVIEW_EXTERNAL_SYMBOL,
    TRADINGVIEW_SOURCE,
    TradingViewBybitSpotAdapter,
)

__all__ = [
    "BYBIT_SOURCE",
    "BybitSpotAdapter",
    "BybitSnapshotSource",
    "Candle",
    "CandleHistory",
    "MarketIdentity",
    "Ticker",
    "FallbackMarketDataProvider",
    "TRADINGVIEW_EXTERNAL_SYMBOL",
    "TRADINGVIEW_SOURCE",
    "TradingViewBybitSpotAdapter",
    "TradingViewSnapshotSource",
    "build_market_snapshot",
]
