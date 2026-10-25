from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from btc_dca_bridge.config import load_execution_config
from btc_dca_bridge.core.models import AccountIdentity
from btc_dca_bridge.execution import (
    NoSubmissionEvidence,
    client_order_id,
    make_order_intent,
    validate_execution_safety,
)
from btc_dca_bridge.execution_v2.cutover_parity import (
    build_cutover_parity_matrix,
    parity_statuses,
)
from btc_dca_bridge.execution_v2.composition import order_intent_from_decision
from btc_dca_bridge.execution_v2.oms import InMemoryOms
from btc_dca_bridge.execution_v2.service import ExecutionService
from btc_dca_bridge.core.models import OrderIntent, Asset, Market, OrderType, Side
from btc_dca_bridge.core.state import ExecutionState, Order
from btc_dca_bridge.risk.engine import RiskContext, RiskEngine
from btc_dca_bridge.execution import ambiguous_submission_policy


DECISION = {
    "strategy_id": "btc_adaptive_dca_v1",
    "strategy_version": "1.0.0",
    "decision_id": "decision-parity",
    "final_purchase_usd": 25,
}
RUN_ID = "run_20261025T000000Z_parity"
CREATED = "2026-10-25T00:00:00Z"


def v2_context(**changes: object) -> RiskContext:
    base = RiskContext(
        "2026-10", Decimal("0"), Decimal("500"), False, True, True,
        AccountIdentity("bybit", "account-1"), "account-1", Decimal("100"),
        False, False, venue_health="healthy",
    )
    return replace(base, **changes)


class DummyInstrumentProvider:
    def get_rules(self, _exchange: str, _market: str, _symbol: str):
        class Rules:
            def validate_quote(self, _amount: Decimal) -> None: pass
        return Rules()


class CutoverParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.legacy = make_order_intent(DECISION, run_id=RUN_ID, created_at_utc=CREATED, calendar_month="2026-10")
        self.v2 = order_intent_from_decision(DECISION, run_id=RUN_ID, created_at_utc=CREATED)

    def test_matrix_proves_safe_quote_buy_invariants(self):
        matrix = build_cutover_parity_matrix(self.legacy, self.v2)
        self.assertEqual(parity_statuses(matrix), {"PARITY"})
        values = {row.semantic: row for row in matrix}
        self.assertEqual(values["venue and market"].v2, ("Bybit", "spot", "BTCUSDT"))
        self.assertEqual(values["quote-denominated amount"].legacy, "25")
        self.assertEqual(values["marketUnit"].legacy, values["marketUnit"].v2)
        self.assertEqual(values["leverage/borrowing"].legacy, values["leverage/borrowing"].v2)

    def test_kill_switch_and_monthly_cap_block_both_semantics(self):
        with TemporaryDirectory() as root:
            ledger = Path(root) / "executions.jsonl"
            ledger.write_text("")
            config = replace(load_execution_config(), live_execution_enabled=True, kill_switch=True)
            legacy = validate_execution_safety(self.legacy, DECISION, ledger_path=ledger, calendar_month="2026-10", execution_config=config, instrument_provider=DummyInstrumentProvider(), submission_state=NoSubmissionEvidence())
            self.assertIn("kill switch is active", legacy.reasons)
        v2 = RiskEngine().evaluate(self.v2, v2_context(kill_switch_active=True))
        self.assertIn("KILL_SWITCH_ACTIVE", v2.reason_codes)
        cap = RiskEngine().evaluate(self.v2, v2_context(monthly_spend=Decimal("500")))
        self.assertIn("MONTHLY_CAP_EXCEEDED", cap.reason_codes)
        with TemporaryDirectory() as root:
            ledger = Path(root) / "executions.jsonl"
            ledger.write_text('{"schema_version":"1.0.0","execution_id":"execution_e","executed_at_utc":"2026-10-01T00:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":500,"reference_price_usdt":1,"btc_quantity":null,"status":"reconciled","reconciliation":{"source":"Project conversation — user-confirmed execution","note":"confirmed"}}\n')
            config = replace(load_execution_config(), live_execution_enabled=True, kill_switch=False)
            legacy = validate_execution_safety(self.legacy, DECISION, ledger_path=ledger, calendar_month="2026-10", execution_config=config, instrument_provider=DummyInstrumentProvider(), submission_state=NoSubmissionEvidence())
            self.assertIn("monthly cap would be exceeded", legacy.reasons)

    def test_spot_no_leverage_duplicate_and_ambiguity_invariants(self):
        self.assertEqual((self.legacy.exchange, self.legacy.market_type, self.legacy.symbol, self.legacy.side), ("Bybit", "spot", "BTCUSDT", "Buy"))
        self.assertEqual((self.v2.preferred_venue, self.v2.market.instrument_type, self.v2.market.canonical_symbol, self.v2.side, self.v2.order_type), ("bybit", "spot", "BTC/USDT", Side.BUY, OrderType.MARKET))
        duplicate = RiskEngine().evaluate(self.v2, v2_context(duplicate_execution_exists=True))
        self.assertIn("DUPLICATE_EXECUTION_IDENTITY", duplicate.reason_codes)
        with TemporaryDirectory() as root:
            self.assertIn("RECONCILIATION REQUIRED", validate_execution_safety(self.legacy, DECISION, ledger_path=Path(root) / "missing", calendar_month="2026-10", execution_config=replace(load_execution_config(), live_execution_enabled=True, kill_switch=False), instrument_provider=DummyInstrumentProvider(), submission_state=type("Ambiguous", (), {"decision_state": lambda *_: "ambiguous", "client_order_state": lambda *_: "ambiguous"})()).reasons)
        self.assertEqual(ambiguous_submission_policy(), "mark ambiguous; reconcile by client_order_id; only retry after conclusive absence")
        self.assertNotIn(ExecutionState.SUBMISSION_STARTED, {ExecutionState.AMBIGUOUS})

    def test_identity_is_deterministic_and_reconciliation_uses_client_identity(self):
        self.assertEqual(self.legacy.client_order_id, client_order_id(DECISION["strategy_id"], DECISION["decision_id"], RUN_ID))
        self.assertEqual(self.legacy.client_order_id, make_order_intent(DECISION, run_id=RUN_ID, created_at_utc=CREATED, calendar_month="2026-10").client_order_id)
        self.assertNotEqual(self.v2.client_order_id, order_intent_from_decision(DECISION, run_id=RUN_ID + "-other", created_at_utc=CREATED).client_order_id)
        rows = {row.semantic: row for row in build_cutover_parity_matrix(self.legacy, self.v2)}
        self.assertEqual(rows["reconciliation identity"].v2, "client_order_id")

    def test_v2_ambiguous_recovery_never_calls_submit_again(self):
        class Venue:
            name = "bybit"
            submits = 0
            def reconcile(self, intent, venue_order_id):
                from btc_dca_bridge.venues.base import VenueReconciliation
                return VenueReconciliation("AMBIGUOUS", venue_order_id, (), "still unresolved")
            def submit_once(self, _intent): self.submits += 1; raise AssertionError("must not resubmit")
        oms = InMemoryOms()
        order = Order(self.v2).transition(ExecutionState.RISK_APPROVED)
        oms.create(order)
        oms.claim_submission(order)
        oms.save(Order(self.v2, ExecutionState.AMBIGUOUS))
        service = ExecutionService(oms=oms, risk_engine=RiskEngine(), venue=Venue())
        result = service.recover_unresolved()
        self.assertEqual(result[0].order.state, ExecutionState.AMBIGUOUS)
        self.assertEqual(service.venue.submits, 0)


if __name__ == "__main__":
    unittest.main()
