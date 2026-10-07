import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from btc_dca_bridge.execution import (
    Fill, InstrumentRules, OrderIntent, client_order_id, reconcile_fills,
    validate_execution_safety, validate_spot_instrument,
)


class ExecutionFoundationTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Path(tempfile.mkdtemp()) / "executions.jsonl"
        self.ledger.write_text("")
        self.decision = {"strategy_id": "btc_adaptive_dca_v1", "strategy_version": "1.0.0",
                         "decision_id": "decision_1", "final_purchase_usd": 10}
        self.intent = OrderIntent("5.1.0", "btc_adaptive_dca_v1", "1.0.0", "run_1",
            "decision_1", "intent_1", "2026-10-07T00:00:00Z", "Bybit", "spot", "BTCUSDT", "Buy",
            Decimal("10"), "recommendation_only", client_order_id("btc_adaptive_dca_v1", "decision_1", "run_1"),
            Decimal("0"), Decimal("500"), "pending", False, False)

    def test_market_rejects_derivatives_and_non_btc(self):
        validate_spot_instrument("Bybit", "spot", "BTCUSDT")
        for symbol in ("BTCUSDT.P", "BTCUSDT-PERP", "ETHUSDT"):
            with self.assertRaises(ValueError): validate_spot_instrument("Bybit", "spot", symbol)

    def test_client_id_is_deterministic_and_distinct(self):
        a = client_order_id("btc_adaptive_dca_v1", "decision_1", "run_1")
        self.assertEqual(a, client_order_id("btc_adaptive_dca_v1", "decision_1", "run_1"))
        self.assertNotEqual(a, client_order_id("btc_adaptive_dca_v1", "decision_2", "run_1"))

    def test_default_guards_fail_closed(self):
        result = validate_execution_safety(self.intent, self.decision, ledger_path=self.ledger, calendar_month="2026-10")
        self.assertEqual(result.status, "rejected")
        self.assertIn("kill switch is active", result.reasons)
        self.assertIn("live execution is disabled", result.reasons)

    def test_double_cap_reads_ledger(self):
        self.ledger.write_text('{"schema_version":"1.0.0","execution_id":"execution_x","executed_at_utc":"2026-10-01T00:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":500,"reference_price_usdt":1,"btc_quantity":null,"status":"reconciled","reconciliation":{"source":"Chat 03 — Portfolio & Budget Tracker","note":"confirmed"}}\n')
        result = validate_execution_safety(self.intent, self.decision, ledger_path=self.ledger, calendar_month="2026-10")
        self.assertIn("monthly cap would be exceeded", result.reasons)

    def test_partial_fills_aggregate_only_confirmed(self):
        result = reconcile_fills("dca-" + "a" * 32, [Fill("1", Decimal(".1"), Decimal("10"), True), Fill("2", Decimal(".1"), Decimal("10"), False)])
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.confirmed_quote_value_usdt, Decimal("10"))

    def test_instrument_rules_reject_without_rounding(self):
        rules = InstrumentRules(Decimal("10"), Decimal("0.00001"), Decimal("0.00001"), Decimal("0.01"), True)
        with self.assertRaises(ValueError): rules.validate_quote(Decimal("9.99"))
