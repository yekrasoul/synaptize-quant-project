from __future__ import annotations

from decimal import Decimal
import unittest

from btc_dca_bridge.core.models import Asset, Market, OrderIntent, OrderType, Side
from btc_dca_bridge.venues.ccxt.adapter import CcxtVenueAdapter


class FakeCcxt:
    def __init__(self, rows): self.rows, self.calls = rows, []
    def fetch_orders(self, symbol, params): self.calls.append((symbol, params)); return self.rows
    def fetch_my_trades(self, symbol, params): return [{"id": "fill-1", "amount": "0.001", "cost": "25", "fee": {"cost": "0.01", "currency": "USDT"}, "timestamp": "2026-10-24T00:00:00Z"}]


def intent():
    return OrderIntent("i", "adaptive_dca_v1", "d", Market(Asset("BTC"), Asset("USDT")), Side.BUY, OrderType.MARKET, Decimal("25"), "bybit", "client-identity", "2026-10-24T00:00:00Z")


class CcxtV2ReconciliationTests(unittest.TestCase):
    def test_missing_ack_order_id_reconciles_by_client_identity(self):
        client = FakeCcxt([{"id": "venue-1", "clientOrderId": "client-identity", "status": "closed"}])
        result = CcxtVenueAdapter("bybit", client, identity_factory=lambda _: "x").reconcile(intent(), None)
        self.assertEqual((result.status, result.venue_order_id, len(result.fills)), ("CONFIRMED", "venue-1", 1))
        self.assertEqual(client.calls, [("BTC/USDT", {"orderLinkId": "client-identity"})])

    def test_absent_or_conflicting_client_evidence_stays_ambiguous(self):
        for rows in ([], [{"id": "a", "clientOrderId": "client-identity"}, {"id": "b", "clientOrderId": "client-identity"}]):
            self.assertEqual(CcxtVenueAdapter("bybit", FakeCcxt(rows), identity_factory=lambda _: "x").reconcile(intent(), None).status, "AMBIGUOUS")


if __name__ == "__main__": unittest.main()
