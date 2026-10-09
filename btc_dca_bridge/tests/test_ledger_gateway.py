import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from btc_dca_bridge.ledger import confirmed_executions, parse_executions, read_executions
from btc_dca_bridge.errors import AmbiguousManualExecutionError, LedgerValidationError
from btc_dca_bridge.ledger_gateway import LedgerGateway
from btc_dca_bridge.ledger_sync import InMemoryVersionedLedgerStore, LedgerReconciliationService, LedgerVersionConflict


class ConcurrentSamePayloadStore(InMemoryVersionedLedgerStore):
    def __init__(self):
        super().__init__()
        self.inject = True

    def compare_and_swap(self, *, expected_version, new_content):
        if self.inject:
            self.inject = False
            current = self.read()
            super().compare_and_swap(expected_version=current.version, new_content=new_content)
        return super().compare_and_swap(expected_version=expected_version, new_content=new_content)


class LedgerGatewayTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ledger = Path(self.directory.name) / "executions.jsonl"
        self.gateway = LedgerGateway(ledger_path=self.ledger)

    def test_record_is_idempotent_across_interfaces(self):
        first_id, first_created = self.gateway.record_execution(
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("25"),
            reference_price_usdt=Decimal("82000"),
            source="Chat 03 — Portfolio & Budget Tracker",
            note="confirmed",
        )
        second_id, second_created = self.gateway.record_execution(
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("25"),
            reference_price_usdt=Decimal("82000"),
            source="This project chat",
            note="same confirmed fill",
        )

        self.assertEqual(first_id, second_id)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(len(read_executions(self.ledger)), 1)

    def test_timestamp_normalization_is_idempotent(self):
        first = self.gateway.record_execution(executed_at_utc="2026-10-09T12:00:00Z", executed_usd=Decimal("25"), reference_price_usdt=Decimal("82000"), source="Chat 03", note="one")
        again = self.gateway.record_execution(executed_at_utc="2026-10-09T13:00:00+01:00", executed_usd=Decimal("25"), reference_price_usdt=Decimal("82000"), source="Codex CLI", note="same")
        self.assertEqual(first[0], again[0]); self.assertTrue(first[1]); self.assertFalse(again[1])

    def test_semantic_duplicate_written_concurrently_by_another_interface_is_idempotent(self):
        store = ConcurrentSamePayloadStore()
        gateway = LedgerGateway(self.ledger, store=store)
        result = gateway.record_execution(executed_at_utc="2026-10-09T12:00:00Z", executed_usd=Decimal("25"), reference_price_usdt=Decimal("82000"), source="Codex CLI", note="user-confirmed")
        self.assertFalse(result[1])
        self.assertEqual(len(confirmed_executions(parse_executions(store.read().content))), 1)

    def test_timestamp_only_economic_repeat_is_ambiguous(self):
        self.gateway.record_execution(executed_at_utc="2026-10-09T12:00:00Z", executed_usd=Decimal("25"), reference_price_usdt=Decimal("82000"), source="Chat 03", note="first")
        before = self.ledger.read_bytes()
        with self.assertRaises(AmbiguousManualExecutionError):
            self.gateway.record_execution(executed_at_utc="2026-10-09T12:00:01Z", executed_usd=Decimal("25"), reference_price_usdt=Decimal("82000"), source="Codex CLI", note="possibly same")
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_explicit_distinct_same_details_requires_and_accepts_distinct_identity(self):
        self.gateway.record_execution(executed_at_utc="2026-10-09T12:00:00Z", executed_usd=Decimal("25"), reference_price_usdt=Decimal("82000"), source="Chat 03", note="first")
        with self.assertRaises(LedgerValidationError):
            self.gateway.record_execution(executed_at_utc="2026-10-09T12:00:01Z", executed_usd=Decimal("25"), reference_price_usdt=Decimal("82000"), source="Codex CLI", note="second", explicit_distinct_execution=True)
        result = self.gateway.record_execution(executed_at_utc="2026-10-09T12:00:01Z", executed_usd=Decimal("25"), reference_price_usdt=Decimal("82000"), source="Codex CLI", note="separate purchase", execution_id="execution_manual_second", explicit_distinct_execution=True)
        self.assertTrue(result[1]); self.assertEqual(len(confirmed_executions(read_executions(self.ledger))), 2)

    def test_conflicting_economics_are_not_merged(self):
        self.gateway.record_execution(executed_at_utc="2026-10-09T12:00:00Z", executed_usd=Decimal("25"), reference_price_usdt=Decimal("82000"), source="Chat 03", note="first")
        with self.assertRaises(LedgerValidationError):
            self.gateway.record_execution(executed_at_utc="2026-10-09T12:00:02Z", executed_usd=Decimal("26"), reference_price_usdt=Decimal("82000"), source="Codex CLI", note="conflicting")

    def test_execution_identity_reuse_across_event_kinds_fails_closed(self):
        original_id, _ = self.gateway.record_execution(executed_at_utc="2026-10-09T12:00:00Z", executed_usd=Decimal("25"), reference_price_usdt=Decimal("82000"), source="Codex CLI", note="purchase")
        correction_id, _ = self.gateway.correct_execution(supersedes_execution_id=original_id, executed_at_utc="2026-10-09T12:00:00Z", executed_usd=Decimal("30"), reference_price_usdt=Decimal("81000"), source="Codex CLI", note="correction")
        with self.assertRaises(LedgerValidationError):
            self.gateway.record_execution(executed_at_utc="2026-10-09T12:00:00Z", executed_usd=Decimal("30"), reference_price_usdt=Decimal("81000"), source="Codex CLI", note="not a new correction", execution_id=correction_id)
        with self.assertRaises(LedgerValidationError):
            self.gateway.cancel_execution(supersedes_execution_id=correction_id, cancelled_at_utc="2026-10-09T12:00:01Z", source="Codex CLI", note="identity collision", execution_id=original_id)

    def test_unknown_provenance_is_rejected(self):
        with self.assertRaises(LedgerValidationError):
            self.gateway.record_execution(executed_at_utc="2026-10-09T12:00:00Z", executed_usd=Decimal("25"), reference_price_usdt=Decimal("82000"), source="arbitrary source", note="x")

    def test_correction_supersedes_without_double_counting(self):
        original_id, _ = self.gateway.record_execution(
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("25"),
            reference_price_usdt=Decimal("82000"),
            source="Chat 03 — Portfolio & Budget Tracker",
            note="initial",
        )
        replacement_id, _ = self.gateway.correct_execution(
            supersedes_execution_id=original_id,
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("30"),
            reference_price_usdt=Decimal("81900"),
            source="Project chat correction",
            note="corrected amount and price",
        )

        active = confirmed_executions(read_executions(self.ledger))
        self.assertEqual([item.execution_id for item in active], [replacement_id])
        self.assertEqual(active[0].executed_usd, Decimal("30"))

    def test_repeated_correction_is_idempotent(self):
        original_id, _ = self.gateway.record_execution(
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("25"),
            reference_price_usdt=Decimal("82000"),
            source="Chat 03 — Portfolio & Budget Tracker",
            note="initial",
        )
        first_id, first_created = self.gateway.correct_execution(
            supersedes_execution_id=original_id,
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("30"),
            reference_price_usdt=Decimal("81900"),
            source="Chat 03",
            note="corrected",
        )
        second_id, second_created = self.gateway.correct_execution(
            supersedes_execution_id=original_id,
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("30"),
            reference_price_usdt=Decimal("81900"),
            source="Another project chat",
            note="same correction repeated",
        )

        self.assertEqual(first_id, second_id)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(len(read_executions(self.ledger)), 2)

    def test_cancel_removes_execution_from_active_projection(self):
        original_id, _ = self.gateway.record_execution(
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("25"),
            reference_price_usdt=Decimal("82000"),
            source="Any BTC DCA project chat",
            note="confirmed",
        )
        self.gateway.cancel_execution(
            supersedes_execution_id=original_id,
            cancelled_at_utc="2026-10-09T13:00:00Z",
            source="Any BTC DCA project chat",
            note="user corrected: purchase did not happen",
        )

        self.assertEqual(confirmed_executions(read_executions(self.ledger)), ())

    def test_repeated_cancel_is_idempotent(self):
        original_id, _ = self.gateway.record_execution(
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("25"),
            reference_price_usdt=Decimal("82000"),
            source="Any BTC DCA project chat",
            note="confirmed",
        )
        first_id, first_created = self.gateway.cancel_execution(
            supersedes_execution_id=original_id,
            cancelled_at_utc="2026-10-09T13:00:00Z",
            source="Chat 03",
            note="void",
        )
        second_id, second_created = self.gateway.cancel_execution(
            supersedes_execution_id=original_id,
            cancelled_at_utc="2026-10-10T09:30:00Z",
            source="Another chat",
            note="same void repeated later",
        )

        self.assertEqual(first_id, second_id)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(len(read_executions(self.ledger)), 2)


    def test_portfolio_state_counts_only_active_correction(self):
        original_id, _ = self.gateway.record_execution(
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("25"),
            reference_price_usdt=Decimal("82000"),
            source="Chat 03 — Portfolio & Budget Tracker",
            note="initial",
        )
        replacement_id, _ = self.gateway.correct_execution(
            supersedes_execution_id=original_id,
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("30"),
            reference_price_usdt=Decimal("81900"),
            source="Project chat correction",
            note="corrected",
        )

        state = self.gateway.get_portfolio_state("2026-10")
        self.assertEqual(state.monthly_confirmed_usd_deployed, Decimal("30"))
        self.assertEqual(state.remaining_monthly_budget_usd, Decimal("470"))
        self.assertEqual(state.monthly_confirmed_execution_count, 1)
        self.assertEqual(state.derived_from_execution_ids, (replacement_id,))

    def test_portfolio_state_excludes_voided_execution(self):
        original_id, _ = self.gateway.record_execution(
            executed_at_utc="2026-10-09T12:00:00Z",
            executed_usd=Decimal("25"),
            reference_price_usdt=Decimal("82000"),
            source="Any BTC DCA project chat",
            note="confirmed",
        )
        self.gateway.cancel_execution(
            supersedes_execution_id=original_id,
            cancelled_at_utc="2026-10-10T09:30:00Z",
            source="Any BTC DCA project chat",
            note="did not happen",
        )

        state = self.gateway.get_portfolio_state("2026-10")
        self.assertEqual(state.monthly_confirmed_usd_deployed, Decimal("0"))
        self.assertEqual(state.remaining_monthly_budget_usd, Decimal("500"))
        self.assertEqual(state.monthly_confirmed_execution_count, 0)
        self.assertEqual(state.derived_from_execution_ids, ())



if __name__ == "__main__":
    unittest.main()
