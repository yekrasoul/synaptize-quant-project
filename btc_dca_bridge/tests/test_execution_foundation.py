import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from decimal import Decimal
from pathlib import Path

from btc_dca_bridge.execution import (
    Fill, JsonInstrumentMetadataProvider, NoSubmissionEvidence, OrderIntent, SubmissionEvidenceStore,
    client_order_id, parse_bybit_spot_instrument_info, reconcile_fills,
    validate_execution_safety, validate_spot_instrument,
)
from btc_dca_bridge.cli import main


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
        self.metadata = {"retCode": 0, "result": {"category": "spot", "list": [{"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "lotSizeFilter": {"minOrderAmt": "10", "minOrderQty": "0.00001", "qtyStep": "0.00001"}, "priceFilter": {"tickSize": "0.01"}, "marketBuyAllowed": True}]}}
        self.provider = JsonInstrumentMetadataProvider(self.metadata)

    def test_market_rejects_derivatives_and_non_btc(self):
        validate_spot_instrument("Bybit", "spot", "BTCUSDT")
        for symbol in ("BTCUSDT.P", "BTCUSDT-PERP", "ETHUSDT"):
            with self.assertRaises(ValueError): validate_spot_instrument("Bybit", "spot", symbol)

    def test_client_id_is_deterministic_and_distinct(self):
        a = client_order_id("btc_adaptive_dca_v1", "decision_1", "run_1")
        self.assertEqual(a, client_order_id("btc_adaptive_dca_v1", "decision_1", "run_1"))
        self.assertEqual(a, client_order_id("btc_adaptive_dca_v1", "decision_1", "run_1"))
        self.assertNotEqual(a, client_order_id("btc_adaptive_dca_v1", "decision_2", "run_1"))

    def test_default_guards_fail_closed(self):
        result = validate_execution_safety(self.intent, self.decision, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider)
        self.assertEqual(result.status, "rejected")
        self.assertIn("kill switch is active", result.reasons)
        self.assertIn("live execution is disabled", result.reasons)

    def test_double_cap_reads_ledger(self):
        self.ledger.write_text('{"schema_version":"1.0.0","execution_id":"execution_x","executed_at_utc":"2026-10-01T00:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":500,"reference_price_usdt":1,"btc_quantity":null,"status":"reconciled","reconciliation":{"source":"Chat 03 — Portfolio & Budget Tracker","note":"confirmed"}}\n')
        result = validate_execution_safety(self.intent, self.decision, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider)
        self.assertIn("monthly cap would be exceeded", result.reasons)

    def test_monthly_cap_edge_cases_reread_canonical_ledger(self):
        record = '{"schema_version":"1.0.0","execution_id":"execution_x","executed_at_utc":"2026-10-01T00:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":%s,"reference_price_usdt":1,"btc_quantity":null,"status":"reconciled","reconciliation":{"source":"Chat 03 — Portfolio & Budget Tracker","note":"confirmed"}}\n'
        for spent, exceeded in (("60", False), ("490", False), ("490.01", True), ("500", True)):
            self.ledger.write_text(record % spent)
            result = validate_execution_safety(self.intent, self.decision, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider)
            self.assertEqual("monthly cap would be exceeded" in result.reasons, exceeded, spent)
        stale = {**self.decision, "monthly_spent_before_usd": "0", "remaining_budget_before_usd": "500"}
        self.ledger.write_text(record % "500")
        result = validate_execution_safety(self.intent, stale, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider)
        self.assertIn("monthly cap would be exceeded", result.reasons)

    def test_submission_state_unknown_fails_closed_and_none_is_not_ledger_absence(self):
        class Unknown:
            def decision_state(self, decision_id): return "mystery"
            def client_order_state(self, client_order_id): return "none"
        result = validate_execution_safety(self.intent, self.decision, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider, submission_state=Unknown())
        self.assertIn("submission evidence state is invalid", result.reasons)
        result = validate_execution_safety(self.intent, self.decision, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider, submission_state=NoSubmissionEvidence())
        self.assertNotIn("previously confirmed", " ".join(result.reasons))

    def test_instrument_identity_and_market_buy_guards(self):
        for exchange, market, symbol in (("Other", "spot", "BTCUSDT"), ("Bybit", "spot", "ETHUSDT"), ("Bybit", "linear", "BTCUSDT")):
            with self.assertRaises(ValueError): self.provider.get_rules(exchange, market, symbol)
        disabled = {**self.metadata, "result": {**self.metadata["result"], "list": [{**self.metadata["result"]["list"][0], "marketBuyAllowed": False}]}}
        with self.assertRaises(ValueError): JsonInstrumentMetadataProvider(disabled).get_rules("Bybit", "spot", "BTCUSDT").validate_quote(Decimal("10"))

    def test_execution_plan_persists_rejected_plan_and_never_orders(self):
        root = Path(tempfile.mkdtemp())
        decision_path = root / "decision.json"
        decision_path.write_text('{"strategy_id":"btc_adaptive_dca_v1","strategy_version":"1.0.0","decision_id":"decision_cli","final_purchase_usd":10}')
        output = StringIO()
        with redirect_stdout(output):
            code = main(["execution-plan", "--decision-json", str(decision_path), "--run-id", "run_20261007T120000Z_a1b2c3d4e5f6", "--created-at", "2026-10-07T12:00:00Z", "--month", "2026-10", "--ledger", str(self.ledger), "--data-root", str(root / "data")])
        text = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("NO ORDER EXECUTED", text)
        self.assertTrue(list((root / "data" / "order_intents").rglob("*.json")))
        self.assertTrue(list((root / "data" / "safety_validations").rglob("*.json")))

    def test_partial_fills_aggregate_only_confirmed(self):
        result = reconcile_fills("dca-" + "a" * 32, [Fill("1", Decimal(".1"), Decimal("10"), True), Fill("2", Decimal(".1"), Decimal("10"), False)])
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.confirmed_quote_value_usdt, Decimal("10"))

    def test_instrument_rules_reject_without_rounding(self):
        with self.assertRaises(ValueError): self.provider.get_rules("Bybit", "spot", "BTCUSDT").validate_quote(Decimal("9.99"))

    def test_exact_decimal_equality_and_invalid_amounts(self):
        for value in ("10", "10.00"):
            self.assertEqual(validate_execution_safety(self.intent, {**self.decision, "final_purchase_usd": value}, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider).reasons.count("amount does not match approved Decision"), 0)
        rejected = validate_execution_safety(self.intent, {**self.decision, "final_purchase_usd": "10.50"}, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider)
        self.assertIn("amount does not match approved Decision", rejected.reasons)
        invalid = validate_execution_safety(self.intent, {**self.decision, "final_purchase_usd": "not-a-number"}, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider)
        self.assertIn("Decision amount is invalid", invalid.reasons)

    def test_month_binding_validates_timestamp(self):
        self.assertNotIn("requested calendar month does not match run context", validate_execution_safety(self.intent, self.decision, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider).reasons)
        self.assertIn("requested calendar month does not match run context", validate_execution_safety(self.intent, self.decision, ledger_path=self.ledger, calendar_month="2026-09", instrument_provider=self.provider).reasons)
        malformed = self.intent.__class__(**{**self.intent.__dict__, "created_at_utc": "not-a-date"})
        self.assertIn("run context timestamp is invalid", validate_execution_safety(malformed, self.decision, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider).reasons)

    def test_submission_evidence_states_are_authoritative(self):
        evidence = Path(tempfile.mkdtemp()) / "submission.jsonl"
        evidence.write_text('{"decision_id":"decision_1","client_order_id":"%s","state":"ambiguous"}\n' % self.intent.client_order_id)
        result = validate_execution_safety(self.intent, self.decision, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider, submission_state=SubmissionEvidenceStore(evidence))
        self.assertIn("RECONCILIATION REQUIRED", result.reasons)
        evidence.write_text('{"decision_id":"decision_1","client_order_id":"%s","state":"confirmed"}\n' % self.intent.client_order_id)
        result = validate_execution_safety(self.intent, self.decision, ledger_path=self.ledger, calendar_month="2026-10", instrument_provider=self.provider, submission_state=SubmissionEvidenceStore(evidence))
        self.assertTrue(any("previously confirmed" in reason for reason in result.reasons))

    def test_instrument_parser_fail_closed(self):
        rules = parse_bybit_spot_instrument_info(self.metadata)
        self.assertEqual(rules.quote_minimum, Decimal("10"))
        for bad in ({}, {**self.metadata, "result": {"category": "linear", "list": []}}, {**self.metadata, "result": {"category": "spot", "list": [{"symbol": "ETHUSDT"}]}}):
            with self.assertRaises(ValueError): parse_bybit_spot_instrument_info(bad)
