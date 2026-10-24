from __future__ import annotations

import unittest
from decimal import Decimal

from btc_dca_bridge.accounting.service import AccountingService
from btc_dca_bridge.core.models import AccountIdentity, Asset, Fill, Market, OrderIntent, OrderType, Side
from btc_dca_bridge.core.state import ExecutionState, Order
from btc_dca_bridge.execution_v2.oms import InMemoryOms
from btc_dca_bridge.execution_v2.service import ExecutionService
from btc_dca_bridge.risk.engine import RiskContext, RiskEngine
from btc_dca_bridge.venues.base import VenueOrderAcknowledgement, VenueReconciliation
from btc_dca_bridge.venues.bybit.mapper import ccxt_order_arguments


def intent() -> OrderIntent:
    return OrderIntent("intent-1", "adaptive_dca_v1", "decision-1", Market(Asset("BTC"), Asset("USDT")), Side.BUY, OrderType.MARKET, Decimal("25"), "bybit", "dca-v2-1", "2026-10-24T00:00:00Z")


def context(**changes) -> RiskContext:
    values = dict(calendar_month="2026-10", monthly_spend=Decimal("100"), monthly_cap=Decimal("500"), runtime_execution_enabled=True, kill_switch_active=False, market_data_fresh=True, account_identity=AccountIdentity("bybit", "account-1"), expected_account_fingerprint="account-1", quote_balance=Decimal("500"), unresolved_execution_exists=False, duplicate_execution_exists=False)
    values.update(changes)
    return RiskContext(**values)


class FakeVenue:
    name = "bybit"
    def __init__(self, *, timeout=False, reconciliation="CONFIRMED"):
        self.timeout, self.reconciliation, self.posts = timeout, reconciliation, 0
    def account_identity(self): return AccountIdentity("bybit", "account-1")
    def quote_balance(self, _): return Decimal("500")
    def validate_intent(self, _): return None
    def submit_once(self, order):
        self.posts += 1
        if self.timeout: raise TimeoutError("socket closed after POST")
        return VenueOrderAcknowledgement("venue-1", order.client_order_id)
    def reconcile(self, order, venue_order_id):
        fill = Fill("fill-1", venue_order_id, Decimal("0.001"), Decimal("25"), Decimal("0.01"), Asset("USDT"), "2026-10-24T00:00:01Z")
        return VenueReconciliation(self.reconciliation, venue_order_id, (fill,) if self.reconciliation == "CONFIRMED" else ())


class ExecutionPlatformV2Tests(unittest.TestCase):
    def test_bybit_ccxt_mapping_contains_only_adapter_specific_fields(self):
        symbol, side, kind, amount, price, params = ccxt_order_arguments(intent())
        self.assertEqual((symbol, side, kind, amount, price), ("BTC/USDT", "buy", "market", Decimal("25"), None))
        self.assertEqual(params, {"marketUnit": "quoteCoin", "isLeverage": 0, "orderLinkId": "dca-v2-1"})

    def test_risk_engine_enforces_cap_kill_switch_identity_and_ambiguity(self):
        engine = RiskEngine()
        self.assertTrue(engine.evaluate(intent(), context()).approved)
        result = engine.evaluate(intent(), context(kill_switch_active=True, unresolved_execution_exists=True, monthly_spend=Decimal("490")))
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(set(result.reason_codes), {"KILL_SWITCH_ACTIVE", "UNRESOLVED_EXECUTION", "MONTHLY_CAP_EXCEEDED"})

    def test_submission_is_exactly_once_and_confirmed_only_after_reconciliation(self):
        venue = FakeVenue()
        service = ExecutionService(oms=InMemoryOms(), risk_engine=RiskEngine(), venue=venue)
        result = service.execute(intent(), context())
        self.assertEqual((result.order.state, venue.posts), (ExecutionState.CONFIRMED, 1))
        with self.assertRaises(ValueError):
            service.execute(intent(), context())

    def test_timeout_is_ambiguous_and_no_automatic_resubmission_is_possible(self):
        venue = FakeVenue(timeout=True)
        oms = InMemoryOms()
        result = ExecutionService(oms=oms, risk_engine=RiskEngine(), venue=venue).execute(intent(), context())
        self.assertEqual((result.order.state, venue.posts), (ExecutionState.AMBIGUOUS, 1))
        with self.assertRaises(ValueError):
            oms.claim_submission(intent().client_order_id)
        with self.assertRaises(ValueError):
            result.order.transition(ExecutionState.SUBMISSION_STARTED)

    def test_accounting_rejects_acknowledgement_and_accepts_confirmed_fill_evidence(self):
        order = Order(intent())
        with self.assertRaises(ValueError):
            AccountingService().post_confirmed(order, VenueReconciliation("CONFIRMED", "venue-1", ()))
        order = order.transition(ExecutionState.RISK_APPROVED).transition(ExecutionState.SUBMISSION_STARTED).transition(ExecutionState.SUBMITTED, venue_order_id="venue-1").transition(ExecutionState.RECONCILING).transition(ExecutionState.CONFIRMED)
        fill = Fill("fill-1", "venue-1", Decimal("0.001"), Decimal("25"), Decimal("0.01"), Asset("USDT"), "2026-10-24T00:00:01Z")
        entry = AccountingService().post_confirmed(order, VenueReconciliation("CONFIRMED", "venue-1", (fill,)))
        self.assertEqual((entry.quote_decrease, entry.base_increase), (Decimal("25"), Decimal("0.001")))


if __name__ == "__main__":
    unittest.main()
