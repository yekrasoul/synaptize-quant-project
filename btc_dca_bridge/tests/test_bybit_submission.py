import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from btc_dca_bridge.artifacts import ArtifactStore
from btc_dca_bridge.bybit_spot_transport import SpotMarketBuyRequest, request_fingerprint
from btc_dca_bridge.bybit_submission import (
    BybitSubmissionReconciler,
    DurableSubmissionJournal,
    ReconciliationResult,
    SubmissionContext,
)
from btc_dca_bridge.private_bybit import ExecutionFill, OrderState, ReadOnlyOrder


NOW = datetime(2026, 10, 10, 17, 0, tzinfo=UTC)


class _FakeReadClient:
    def __init__(self, order, fills=()):
        self.order = order
        self.fills = tuple(fills)

    def lookup_order(self, client_order_id):
        return self.order

    def executions(self, client_order_id):
        return self.fills


def _request(suffix="a"):
    return SpotMarketBuyRequest(Decimal("25"), "dca-" + suffix * 32)


def _context(suffix="a"):
    return SubmissionContext(
        "canary-" + suffix * 32,
        "approval-" + suffix * 32,
        suffix * 64,
        "decision-" + suffix,
        "intent-" + suffix,
        "run_20261010T170000Z_context" + suffix,
    )


def _order(request, state=OrderState.FILLED, order_id="order-1", qty="0.001", value="25"):
    return ReadOnlyOrder(request.client_order_id, order_id, state, state.value, Decimal(qty), Decimal(value))


def _fill(request, execution_id, qty, value, order_id="order-1"):
    return ExecutionFill(Decimal(qty), Decimal(value) / Decimal(qty), Decimal(value), Decimal("0.01"), "USDT", "2026-10-10T17:00:01Z", execution_id, order_id, request.client_order_id, "spot", "BTCUSDT")


class BybitSubmissionPersistenceTests(unittest.TestCase):
    def test_pre_post_attempt_is_durable_and_restart_blocks_identity(self):
        request = _request("a")
        with tempfile.TemporaryDirectory() as root:
            journal = DurableSubmissionJournal(root)
            journal.persist_before_post(request, _context("a"), attempt_id="attempt-a", now=NOW)
            self.assertTrue(journal.has_prior_attempt(request.client_order_id))
            self.assertTrue(journal.has_unresolved_attempt())
            files = list((Path(root) / "order_submission_attempts").rglob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].with_name(files[0].name + ".sha256").exists())
            self.assertEqual(ArtifactStore(root).read("order_submission_attempt", run_id=files[0].stem, artifact_date_utc=NOW)["request_fingerprint"], request_fingerprint(request))

    def test_acknowledgement_is_not_fill_and_post_outcome_is_persisted(self):
        request = _request("b")
        with tempfile.TemporaryDirectory() as root:
            journal = DurableSubmissionJournal(root)
            context = _context("b")
            journal.persist_before_post(request, context, attempt_id="attempt-b", now=NOW)
            journal.persist_outcome(request, context, attempt_id="attempt-b", now=NOW, state="ACCEPTED", ret_code=0, ret_msg="OK", order_id="order-b", reconciliation_state="ACCEPTED")
            outcomes = list((Path(root) / "order_submission_outcomes").rglob("*.json"))
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(ArtifactStore(root).read("order_submission_outcome", run_id=outcomes[0].stem, artifact_date_utc=NOW)["reconciliation_state"], "ACCEPTED")
            self.assertFalse((Path(root) / "ledger").exists())

    def test_filled_aggregates_multiple_executions(self):
        request = _request("c")
        fills = (_fill(request, "exec-1", "0.0004", "10"), _fill(request, "exec-2", "0.0006", "15"))
        result = BybitSubmissionReconciler(_FakeReadClient(_order(request), fills)).reconcile(request.client_order_id, expected_order_id="order-1")
        self.assertEqual(result.state, "FILLED")
        self.assertEqual(result.executed_btc, Decimal("0.0010"))
        self.assertEqual(result.executed_usdt, Decimal("25"))
        self.assertEqual(result.execution_ids, ("exec-1", "exec-2"))
        self.assertTrue(result.ledger_confirmation_allowed)

    def test_partial_cancelled_and_rejected_never_allow_ledger_confirmation(self):
        request = _request("d")
        for state in (OrderState.PARTIALLY_FILLED, OrderState.CANCELLED, OrderState.REJECTED):
            result = BybitSubmissionReconciler(_FakeReadClient(_order(request, state=state), (_fill(request, "exec-d", "0.0005", "12.5"),) if state is OrderState.PARTIALLY_FILLED else ())).reconcile(request.client_order_id)
            self.assertEqual(result.state, {OrderState.PARTIALLY_FILLED: "PARTIALLY_FILLED", OrderState.CANCELLED: "CANCELLED", OrderState.REJECTED: "REJECTED"}[state])
            self.assertFalse(result.ledger_confirmation_allowed)

    def test_timeout_no_order_is_submission_unknown(self):
        request = _request("e")
        result = BybitSubmissionReconciler(_FakeReadClient(_order(request, OrderState.NOT_FOUND, order_id=None, qty="0", value="0"))).reconcile(request.client_order_id)
        self.assertEqual(result.state, "SUBMISSION_UNKNOWN")
        self.assertFalse(result.ledger_confirmation_allowed)

    def test_conflicting_order_identity_is_unresolved(self):
        request = _request("f")
        result = BybitSubmissionReconciler(_FakeReadClient(_order(request, order_id="order-other"))).reconcile(request.client_order_id, expected_order_id="order-expected")
        self.assertEqual(result.state, "UNRESOLVED")
        self.assertFalse(result.ledger_confirmation_allowed)

    def test_filled_quantity_must_match_intended_quote_amount(self):
        request = _request("e")
        fills = (_fill(request, "exec-e", "0.001", "25"),)
        result = BybitSubmissionReconciler(_FakeReadClient(_order(request), fills)).reconcile(request.client_order_id, expected_order_id="order-1", expected_quote_amount=Decimal("26"))
        self.assertEqual(result.state, "UNRESOLVED")
        self.assertFalse(result.ledger_confirmation_allowed)

    def test_persisted_unresolved_reconciliation_does_not_mutate_ledger(self):
        request = _request("a")
        result = ReconciliationResult("UNRESOLVED", request.client_order_id, None, request.client_order_id, Decimal("0"), Decimal("0"), (), (), "UNKNOWN", "not found")
        with tempfile.TemporaryDirectory() as root:
            journal = DurableSubmissionJournal(root)
            journal.persist_reconciliation(request, _context("a"), attempt_id="attempt-g", now=NOW, result=result)
            self.assertFalse((Path(root) / "ledger").exists())


if __name__ == "__main__":
    unittest.main()
