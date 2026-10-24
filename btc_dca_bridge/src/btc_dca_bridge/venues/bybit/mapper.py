"""The sole current-generation mapping of a canonical order to Bybit via CCXT."""
from __future__ import annotations

from decimal import Decimal

from ...core.models import OrderIntent, OrderType, Side


def ccxt_order_arguments(intent: OrderIntent) -> tuple[str, str, str, Decimal, None, dict[str, object]]:
    if intent.market.canonical_symbol != "BTC/USDT" or intent.market.instrument_type != "spot":
        raise ValueError("current Bybit adapter supports BTC/USDT Spot only")
    if intent.side is not Side.BUY or intent.order_type is not OrderType.MARKET:
        raise ValueError("current Bybit adapter supports market buys only")
    # CCXT keeps venue-native request properties in adapter params, never Core.
    return ("BTC/USDT", "buy", "market", intent.quote_quantity, None, {"marketUnit": "quoteCoin", "isLeverage": 0, "orderLinkId": intent.client_order_id})
