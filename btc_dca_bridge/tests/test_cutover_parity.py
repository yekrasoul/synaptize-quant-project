from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from btc_dca_bridge.config import load_execution_config
from btc_dca_bridge.core.models import AccountIdentity
from btc_dca_bridge.core.state import ExecutionState, Order
from btc_dca_bridge.execution import (
    NoSubmissionEvidence,
    ambiguous_submission_policy,
    client_order_id,
    make_order_intent,
    validate_execution_safety,
)
from btc_dca_bridge.execution_v2.composition import order_intent_from_decision
from btc_dca_bridge.execution_v2.cutover_parity import (
    INTENTIONAL_V2_REPLACEMENT,
    MISMATCH,
    PARITY,
    build_cutover_parity_matrix,
    parity_statuses,
)
from btc_dca_bridge.execution_v2.oms import InMemoryOms
from btc_dca_bridge.execution_v2.service import ExecutionService
from btc_dca_bridge.live_order import LiveOrderEngine, ReconciliationEvidence
from btc_dca_bridge.market_data.http import HttpResponse
from btc_dca_bridge.risk.engine import MarketEvidenceBinding, RiskContext, RiskEngine
from btc_dca_bridge.venues.base import (
    DeterministicVenueRejection,
    VenueReconciliation,
)


DECISION = {
    "strategy_id": "btc_adaptive_dca_v1",
    "strategy_version": "1.0.0",
    "decision_id": "decision-parity",
    "final_purchase_usd": 25,
    "market_snapshot_id": "market-parity",
    "market_snapshot_sha256": "a" * 64,
    "sentiment_snapshot_id": "sentiment-parity",
    "sentiment_snapshot_sha256": "b" * 64,
    "fear_greed_index": 35,
}
RUN_ID = "run_20261025T000000Z_parity"
CREATED = "2026-10-25T00:00:00Z"


def v2_context(**changes: object) -> RiskContext:
    base = RiskContext(
        "2026-10",
        Decimal("0"),
        Decimal("500"),
        True,
        False,
        True,
        AccountIdentity("bybit", "account-1"),
        "account-1",
        Decimal("100"),
        False,
        False,
        venue_health="healthy",
        market_evidence=MarketEvidenceBinding("decision-parity", "market-parity", "a" * 64, "sentiment-parity", "b" * 64, True, 35),
    )
    return replace(base, **changes)


class DummyInstrumentProvider:
    def get_rules(self, _exchange: str, _market: str, _symbol: str):
        class Rules:
            def validate_quote(self, _amount: Decimal) -> None:
                pass

        return Rules()


class SubmissionState:
    def __init__(self, state: str) -> None:
        self.state = state

    def decision_state(self, _decision_id: str) -> str:
        return "none"

    def client_order_state(self, _client_order_id: str) -> str:
        return self.state


class NoSubmitVenue:
    name = "bybit"

    def __init__(self) -> None:
        self.submits = 0
        self.reconciliations: list[tuple[str, str | None]] = []

    def validate_intent(self, _intent) -> None:
        pass

    def prepare_submission(self, _intent):
        return {}

    def submit_once(self, _intent):
        self.submits += 1
        raise AssertionError("submission must not occur")

    def reconcile(self, intent, venue_order_id):
        self.reconciliations.append((intent.client_order_id, venue_order_id))
        return VenueReconciliation("AMBIGUOUS", venue_order_id, (), "still unresolved")


class RejectingVenue(NoSubmitVenue):
    def submit_once(self, _intent):
        self.submits += 1
        raise DeterministicVenueRejection("fixture deterministic rejection")


class RecordingLegacyReconciler:
    def __init__(self) -> None:
        self.client_ids: list[str] = []

    def reconcile_after_ack(self, client_order_id: str) -> ReconciliationEvidence:
        self.client_ids.append(client_order_id)
        return ReconciliationEvidence(
            "ambiguous",
            order_id=None,
            fills=(),
            order_link_id=client_order_id,
        )


class CutoverParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.legacy = make_order_intent(
            DECISION,
            run_id=RUN_ID,
            created_at_utc=CREATED,
            calendar_month="2026-10",
        )
        self.v2 = order_intent_from_decision(
            DECISION,
            run_id=RUN_ID,
            created_at_utc=CREATED,
        )

    def _legacy_config(self, *, kill_switch: bool = False):
        return replace(
            load_execution_config(),
            live_execution_enabled=True,
            kill_switch=kill_switch,
        )

    def test_matrix_derives_safe_quote_buy_parity(self):
        matrix = build_cutover_parity_matrix(self.legacy, self.v2)
        self.assertEqual(
            parity_statuses(matrix),
            {PARITY, INTENTIONAL_V2_REPLACEMENT},
        )
        rows = {row.semantic: row for row in matrix}
        for semantic in (
            "venue and market",
            "side",
            "order type",
            "quote-denominated amount",
            "marketUnit",
            "leverage/borrowing",
        ):
            self.assertEqual(rows[semantic].status, PARITY)
        self.assertEqual(
            rows["canonical client identity"].status,
            INTENTIONAL_V2_REPLACEMENT,
        )

    def test_negative_control_reports_mismatch(self):
        altered = replace(self.v2, quote_quantity=Decimal("26"))
        rows = {
            row.semantic: row
            for row in build_cutover_parity_matrix(self.legacy, altered)
        }
        self.assertEqual(rows["quote-denominated amount"].status, MISMATCH)

    def test_kill_switch_and_monthly_cap_block_both_semantics(self):
        with TemporaryDirectory() as root:
            ledger = Path(root) / "executions.jsonl"
            ledger.write_text("")
            legacy = validate_execution_safety(
                self.legacy,
                DECISION,
                ledger_path=ledger,
                calendar_month="2026-10",
                execution_config=self._legacy_config(kill_switch=True),
                instrument_provider=DummyInstrumentProvider(),
                submission_state=NoSubmissionEvidence(),
            )
            self.assertIn("kill switch is active", legacy.reasons)

        v2_kill = RiskEngine().evaluate(
            self.v2, v2_context(kill_switch_active=True)
        )
        self.assertIn("KILL_SWITCH_ACTIVE", v2_kill.reason_codes)

        v2_cap = RiskEngine().evaluate(
            self.v2, v2_context(monthly_spend=Decimal("500"))
        )
        self.assertIn("MONTHLY_CAP_EXCEEDED", v2_cap.reason_codes)

        with TemporaryDirectory() as root:
            ledger = Path(root) / "executions.jsonl"
            ledger.write_text(
                '{"schema_version":"1.0.0","execution_id":"execution_e",'
                '"executed_at_utc":"2026-10-01T00:00:00Z","asset":"BTC",'
                '"quote_currency":"USDT","executed_usd":500,'
                '"reference_price_usdt":1,"btc_quantity":null,'
                '"status":"reconciled","reconciliation":'
                '{"source":"Project conversation — user-confirmed execution",'
                '"note":"confirmed"}}\n'
            )
            legacy = validate_execution_safety(
                self.legacy,
                DECISION,
                ledger_path=ledger,
                calendar_month="2026-10",
                execution_config=self._legacy_config(),
                instrument_provider=DummyInstrumentProvider(),
                submission_state=NoSubmissionEvidence(),
            )
            self.assertIn("monthly cap would be exceeded", legacy.reasons)

    def test_duplicate_identity_blocks_legacy_and_v2_without_submit(self):
        with TemporaryDirectory() as root:
            ledger = Path(root) / "executions.jsonl"
            ledger.write_text("")
            legacy = validate_execution_safety(
                self.legacy,
                DECISION,
                ledger_path=ledger,
                calendar_month="2026-10",
                execution_config=self._legacy_config(),
                instrument_provider=DummyInstrumentProvider(),
                submission_state=SubmissionState("confirmed"),
            )
            self.assertIn(
                "client_order_id was previously confirmed",
                legacy.reasons,
            )

        venue = NoSubmitVenue()
        oms = InMemoryOms()
        oms.create(Order(self.v2))
        result = ExecutionService(
            oms=oms,
            risk_engine=RiskEngine(),
            venue=venue,
        ).execute(self.v2, v2_context())
        self.assertEqual(
            result.risk_status,
            "DUPLICATE_OR_RECONCILIATION_REQUIRED",
        )
        self.assertIn("DUPLICATE_EXECUTION_IDENTITY", result.reason_codes)
        self.assertEqual(venue.submits, 0)

    def test_ambiguous_identity_requires_reconciliation_and_never_resubmits(self):
        with TemporaryDirectory() as root:
            legacy = validate_execution_safety(
                self.legacy,
                DECISION,
                ledger_path=Path(root) / "missing",
                calendar_month="2026-10",
                execution_config=self._legacy_config(),
                instrument_provider=DummyInstrumentProvider(),
                submission_state=SubmissionState("ambiguous"),
            )
            self.assertIn("REJECT NEW SUBMISSION", legacy.reasons)
            self.assertIn("RECONCILIATION REQUIRED", legacy.reasons)

        self.assertEqual(
            ambiguous_submission_policy(),
            "mark ambiguous; reconcile by client_order_id; only retry after conclusive absence",
        )

        venue = NoSubmitVenue()
        oms = InMemoryOms()
        order = Order(self.v2).transition(ExecutionState.RISK_APPROVED)
        oms.create(order)
        oms.claim_submission(order)
        oms.save(Order(self.v2, ExecutionState.AMBIGUOUS))
        result = ExecutionService(
            oms=oms,
            risk_engine=RiskEngine(),
            venue=venue,
        ).recover_unresolved()
        self.assertEqual(result[0].order.state, ExecutionState.AMBIGUOUS)
        self.assertEqual(venue.submits, 0)
        self.assertEqual(
            venue.reconciliations,
            [(self.v2.client_order_id, None)],
        )

    def test_deterministic_rejection_is_terminal_on_both_paths(self):
        legacy_engine = LiveOrderEngine(artifact_store=object())
        legacy_outcome, legacy_evidence = legacy_engine._parse_response(
            self.legacy,
            RUN_ID,
            HttpResponse(400, {}, b"{}"),
            object(),
            canary_id="canary-parity",
            approval_id="approval-parity",
        )
        self.assertEqual(legacy_outcome.state, "rejected_by_exchange")
        self.assertEqual(legacy_outcome.outcome_category, "exchange_rejected")
        self.assertIsNone(legacy_evidence)

        venue = RejectingVenue()
        result = ExecutionService(
            oms=InMemoryOms(),
            risk_engine=RiskEngine(),
            venue=venue,
        ).execute(self.v2, v2_context())
        self.assertEqual(result.order.state, ExecutionState.REJECTED)
        self.assertEqual(result.risk_status, "REJECTED")
        self.assertIn("VENUE_REJECTED", result.reason_codes)
        self.assertEqual(venue.submits, 1)

    def test_reconciliation_without_venue_id_uses_client_identity_on_both_paths(self):
        reconciler = RecordingLegacyReconciler()
        legacy_engine = LiveOrderEngine(artifact_store=object())
        legacy_outcome, _ = legacy_engine._reconcile_after_possible_submission(
            self.legacy,
            RUN_ID,
            reconciler,
            canary_id="canary-parity",
            approval_id="approval-parity",
        )
        self.assertEqual(reconciler.client_ids, [self.legacy.client_order_id])
        self.assertEqual(
            legacy_outcome.returned_order_link_id,
            self.legacy.client_order_id,
        )
        self.assertEqual(legacy_outcome.reconciliation_state, "ambiguous")

        venue = NoSubmitVenue()
        oms = InMemoryOms()
        oms.create(Order(self.v2, ExecutionState.AMBIGUOUS))
        recovered = ExecutionService(
            oms=oms,
            risk_engine=RiskEngine(),
            venue=venue,
        ).recover_unresolved()
        self.assertEqual(recovered[0].order.state, ExecutionState.AMBIGUOUS)
        self.assertEqual(
            venue.reconciliations,
            [(self.v2.client_order_id, None)],
        )
        self.assertEqual(venue.submits, 0)

    def test_identity_generation_is_deterministic_per_decision_and_run(self):
        self.assertEqual(
            self.legacy.client_order_id,
            client_order_id(
                DECISION["strategy_id"],
                DECISION["decision_id"],
                RUN_ID,
            ),
        )
        self.assertEqual(
            self.legacy.client_order_id,
            make_order_intent(
                DECISION,
                run_id=RUN_ID,
                created_at_utc=CREATED,
                calendar_month="2026-10",
            ).client_order_id,
        )
        self.assertEqual(
            self.v2.client_order_id,
            order_intent_from_decision(
                DECISION,
                run_id=RUN_ID,
                created_at_utc=CREATED,
            ).client_order_id,
        )
        self.assertNotEqual(
            self.v2.client_order_id,
            order_intent_from_decision(
                DECISION,
                run_id=RUN_ID + "-other",
                created_at_utc=CREATED,
            ).client_order_id,
        )


if __name__ == "__main__":
    unittest.main()
