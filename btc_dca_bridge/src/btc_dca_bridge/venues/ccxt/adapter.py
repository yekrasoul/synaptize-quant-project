"""CCXT implementation of the venue port.

CCXT is contained here.  Core and strategy modules never import its types.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Mapping

from ...core.models import AccountIdentity, Asset, Fill, OrderIntent
from ..base import VenueAdapter, VenueOrderAcknowledgement, VenueReconciliation


class CcxtVenueAdapter(VenueAdapter):
    def __init__(self, name: str, client: Any, *, identity_factory: Callable[[Mapping[str, Any]], str]) -> None:
        self.name = name
        self._client = client
        self._identity_factory = identity_factory

    @classmethod
    def bybit(cls, credentials: Mapping[str, str], *, identity_factory: Callable[[Mapping[str, Any]], str]) -> "CcxtVenueAdapter":
        """Construct the current CEX adapter; no network occurs until a method is called."""
        import ccxt  # Dependency boundary: never import this outside venues/ccxt.
        client = ccxt.bybit({"apiKey": credentials["api_key"], "secret": credentials["api_secret"], "enableRateLimit": True, "options": {"defaultType": "spot"}})
        return cls("bybit", client, identity_factory=identity_factory)

    def account_identity(self) -> AccountIdentity:
        balance = self._client.fetch_balance()
        return AccountIdentity(self.name, self._identity_factory(balance))

    def quote_balance(self, quote_asset: str) -> Decimal:
        balance = self._client.fetch_balance()
        return Decimal(str(balance.get("free", {}).get(quote_asset, 0)))

    def validate_intent(self, intent: OrderIntent) -> None:
        market = self._client.market(intent.market.canonical_symbol)
        limits = market.get("limits", {}).get("cost", {})
        minimum = limits.get("min")
        if minimum is not None and intent.quote_quantity < Decimal(str(minimum)):
            raise ValueError("venue minimum notional is not met")

    def submit_once(self, intent: OrderIntent) -> VenueOrderAcknowledgement:
        # Bybit's quote-denominated Market Buy mapping is isolated in its mapper.
        from ..bybit.mapper import ccxt_order_arguments
        symbol, side, order_type, amount, price, params = ccxt_order_arguments(intent)
        response = self._client.create_order(symbol, order_type, side, amount, price, params)
        order_id = str(response.get("id") or "")
        if not order_id:
            raise RuntimeError("venue acknowledgement has no order id")
        return VenueOrderAcknowledgement(order_id, intent.client_order_id)

    def reconcile(self, intent: OrderIntent, venue_order_id: str | None) -> VenueReconciliation:
        if not venue_order_id:
            return VenueReconciliation("AMBIGUOUS", None, (), "missing venue order identity")
        order = self._client.fetch_order(venue_order_id, intent.market.canonical_symbol)
        status = str(order.get("status", "")).lower()
        raw_fills = self._client.fetch_my_trades(intent.market.canonical_symbol, params={"orderId": venue_order_id})
        fills = tuple(
            Fill(str(row["id"]), venue_order_id, Decimal(str(row["amount"])), Decimal(str(row["cost"])), Decimal(str((row.get("fee") or {}).get("cost", 0))), Asset(str((row.get("fee") or {}).get("currency", intent.market.quote.code)).upper()), str(row.get("timestamp", "")))
            for row in raw_fills
        )
        mapped = {"closed": "CONFIRMED", "canceled": "CANCELLED", "rejected": "REJECTED", "open": "PARTIALLY_FILLED" if fills else "RECONCILING"}.get(status, "AMBIGUOUS")
        return VenueReconciliation(mapped, venue_order_id, fills, None if mapped != "AMBIGUOUS" else "unrecognized venue order state")
