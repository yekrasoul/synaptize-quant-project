"""CCXT implementation of the venue port.

CCXT is contained here.  Core and strategy modules never import its types.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Mapping

from ...core.models import AccountIdentity, Asset, Fill, OrderIntent
from ..base import AmbiguousSubmissionError, DeterministicVenueRejection, VenueAdapter, VenueOrderAcknowledgement, VenueReconciliation


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
        # A quote-denominated Bybit market buy must not silently be rounded by
        # the transport.  If CCXT exposes its venue precision helper, reject a
        # non-representable cost before the durable claim/network boundary.
        cost_to_precision = getattr(self._client, "cost_to_precision", None)
        if callable(cost_to_precision):
            normalized = Decimal(str(cost_to_precision(intent.market.canonical_symbol, str(intent.quote_quantity))))
            if normalized != intent.quote_quantity:
                raise ValueError("venue quote amount precision is not met")

    def submit_once(self, intent: OrderIntent) -> VenueOrderAcknowledgement:
        prepared = self.prepare_submission(intent)
        symbol, side, order_type, amount, price, params = prepared["symbol"], prepared["side"], prepared["type"], prepared["amount"], prepared["price"], prepared["params"]
        try:
            response = self._client.create_order(symbol, order_type, side, amount, price, params)
        except self._deterministic_rejection_types() as exc:
            # These CCXT exception classes conclusively mean the venue did not
            # accept this order.  They are deliberately handled inside the
            # venue adapter, never exposed to Core.
            raise DeterministicVenueRejection(str(exc)) from exc
        except Exception as exc:
            # CCXT/network exceptions are uncertain unless an adapter-specific
            # caller has established a deterministic venue rejection.
            raise AmbiguousSubmissionError(str(exc)) from exc
        order_id = str(response.get("id") or "")
        if not order_id:
            raise AmbiguousSubmissionError("venue acknowledgement has no order id")
        return VenueOrderAcknowledgement(order_id, intent.client_order_id)

    def prepare_submission(self, intent: OrderIntent) -> Mapping[str, Any]:
        # Bybit's quote-denominated Market Buy mapping is isolated in its mapper.
        from ..bybit.mapper import ccxt_order_arguments
        symbol, side, order_type, amount, price, params = ccxt_order_arguments(intent)
        return {"symbol": symbol, "side": side, "type": order_type, "amount": amount, "price": price, "params": params}

    @staticmethod
    def _deterministic_rejection_types() -> tuple[type[BaseException], ...]:
        """The small CCXT error allow-list safe to classify before recovery."""
        import ccxt
        return (ccxt.InvalidOrder, ccxt.InsufficientFunds)

    def reconcile(self, intent: OrderIntent, venue_order_id: str | None) -> VenueReconciliation:
        if venue_order_id:
            order = self._client.fetch_order(venue_order_id, intent.market.canonical_symbol)
        else:
            # Bybit exposes orderLinkId; query by canonical client identity when
            # acknowledgement was lost. Absence is ambiguous, never a retry cue.
            rows = self._client.fetch_orders(intent.market.canonical_symbol, params={"orderLinkId": intent.client_order_id})
            matches = [row for row in rows if str(row.get("clientOrderId") or row.get("orderLinkId") or (row.get("info") or {}).get("orderLinkId") or "") == intent.client_order_id]
            if len(matches) != 1:
                return VenueReconciliation("AMBIGUOUS", None, (), "client identity evidence is absent or conflicting")
            order = matches[0]
            venue_order_id = str(order.get("id") or "") or None
            if venue_order_id is None:
                return VenueReconciliation("AMBIGUOUS", None, (), "matching client identity has no venue order id")
        status = str(order.get("status", "")).lower()
        raw_fills = self._client.fetch_my_trades(intent.market.canonical_symbol, params={"orderId": venue_order_id, "orderLinkId": intent.client_order_id})
        fills = tuple(
            Fill(str(row["id"]), venue_order_id, Decimal(str(row["amount"])), Decimal(str(row["cost"])), Decimal(str((row.get("fee") or {}).get("cost", 0))), Asset(str((row.get("fee") or {}).get("currency", intent.market.quote.code)).upper()), str(row.get("timestamp", "")))
            for row in raw_fills
        )
        mapped = {"closed": "CONFIRMED", "canceled": "CANCELLED", "rejected": "REJECTED", "open": "PARTIALLY_FILLED" if fills else "RECONCILING"}.get(status, "AMBIGUOUS")
        return VenueReconciliation(mapped, venue_order_id, fills, None if mapped != "AMBIGUOUS" else "unrecognized venue order state")
