from __future__ import annotations

from decimal import Decimal
import unittest

from btc_dca_bridge.core.models import Asset, Market, OrderIntent, OrderType, Side
from btc_dca_bridge.venues.ccxt.adapter import CcxtVenueAdapter
from btc_dca_bridge.venues.bybit.mapper import ccxt_order_arguments
from btc_dca_bridge.venues.base import AmbiguousSubmissionError, DeterministicVenueRejection
from btc_dca_bridge.bybit_spot_transport import SpotMarketBuyRequest


class FakeCcxt:
    def __init__(self, rows): self.rows, self.calls = rows, []
    def fetch_orders(self, symbol, params): self.calls.append((symbol, params)); return self.rows
    def fetch_my_trades(self, symbol, params): return [{"id": "fill-1", "amount": "0.001", "cost": "25", "fee": {"cost": "0.01", "currency": "USDT"}, "timestamp": "2026-10-24T00:00:00Z"}]


class SubmitCcxt:
    def __init__(self, outcome=None, *, minimum="10", precision=None):
        self.outcome, self.minimum, self.precision, self.calls = outcome, minimum, precision, []
    def market(self, _): return {"limits": {"cost": {"min": self.minimum}}}
    def cost_to_precision(self, _, amount): return self.precision if self.precision is not None else amount
    def create_order(self, *args):
        self.calls.append(args)
        if isinstance(self.outcome, BaseException): raise self.outcome
        return self.outcome or {"id": "venue-1"}


def intent():
    return OrderIntent("i", "adaptive_dca_v1", "d", Market(Asset("BTC"), Asset("USDT")), Side.BUY, OrderType.MARKET, Decimal("25"), "bybit", "client-identity", "2026-10-24T00:00:00Z")


class CcxtV2ReconciliationTests(unittest.TestCase):
    def test_v2_mapping_matches_trusted_bybit_spot_observable_contract(self):
        legacy = SpotMarketBuyRequest(Decimal("25"), "client-identity").to_payload()
        symbol, side, kind, amount, _, params = ccxt_order_arguments(intent())
        self.assertEqual((legacy["category"], legacy["symbol"], legacy["side"], legacy["orderType"], legacy["qty"], legacy["marketUnit"], legacy["isLeverage"], legacy["orderLinkId"]), ("spot", "BTCUSDT", "Buy", "Market", str(amount), "quoteCoin", 0, params["orderLinkId"]))
        self.assertEqual((symbol, side, kind), ("BTC/USDT", "buy", "market"))
    def test_missing_ack_order_id_reconciles_by_client_identity(self):
        client = FakeCcxt([{"id": "venue-1", "clientOrderId": "client-identity", "status": "closed"}])
        result = CcxtVenueAdapter("bybit", client, identity_factory=lambda _: "x").reconcile(intent(), None)
        self.assertEqual((result.status, result.venue_order_id, len(result.fills)), ("CONFIRMED", "venue-1", 1))
        self.assertEqual(client.calls, [("BTC/USDT", {"orderLinkId": "client-identity"})])

    def test_absent_or_conflicting_client_evidence_stays_ambiguous(self):
        for rows in ([], [{"id": "a", "clientOrderId": "client-identity"}, {"id": "b", "clientOrderId": "client-identity"}]):
            self.assertEqual(CcxtVenueAdapter("bybit", FakeCcxt(rows), identity_factory=lambda _: "x").reconcile(intent(), None).status, "AMBIGUOUS")

    def test_native_minimum_and_quote_precision_are_checked_before_submission(self):
        for client in (SubmitCcxt(minimum="26"), SubmitCcxt(precision="25.0"), SubmitCcxt(precision="24.99")):
            adapter = CcxtVenueAdapter("bybit", client, identity_factory=lambda _: "x")
            if client.minimum == "26" or client.precision == "24.99":
                with self.assertRaises(ValueError): adapter.validate_intent(intent())
                self.assertEqual(client.calls, [])
            else:
                adapter.validate_intent(intent())

    def test_deterministic_rejection_and_uncertain_error_have_distinct_contracts(self):
        import ccxt
        rejected = CcxtVenueAdapter("bybit", SubmitCcxt(ccxt.InvalidOrder("invalid")), identity_factory=lambda _: "x")
        with self.assertRaises(DeterministicVenueRejection): rejected.submit_once(intent())
        uncertain = CcxtVenueAdapter("bybit", SubmitCcxt(TimeoutError("lost after POST")), identity_factory=lambda _: "x")
        with self.assertRaises(AmbiguousSubmissionError): uncertain.submit_once(intent())


if __name__ == "__main__": unittest.main()
