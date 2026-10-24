from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from btc_dca_bridge.accounting.deployment import confirmed_monthly_spend
from btc_dca_bridge.accounting.service import LedgerEntry
from btc_dca_bridge.infrastructure.accounting_repository import JsonAccountingRepository
from btc_dca_bridge.models import Execution


def legacy(identity="legacy", when="2026-10-01T00:00:00Z", amount=25, *, status="reconciled", supersedes=None):
    payload = {
        "execution_id": identity,
        "executed_at_utc": when,
        "executed_usd": amount,
        "status": status,
    }
    if supersedes is not None:
        payload["supersedes_execution_id"] = supersedes
    return Execution(payload)


def v2(identity, when, amount, *, outcome="CONFIRMED"):
    return LedgerEntry(identity, Decimal(str(amount)), Decimal("0"), Decimal("0"), when, outcome)


class ConfirmedDeploymentTests(unittest.TestCase):
    def test_legacy_correction_void_and_v2_dedup_use_canonical_active_set(self):
        history = [
            legacy("original", amount=20),
            legacy("corrected", amount=30, supersedes="original"),
            legacy("void-target", when="2026-10-02T00:00:00Z", amount=40),
            legacy("void-event", when="2026-10-02T01:00:00Z", amount=0, status="voided", supersedes="void-target"),
        ]
        rows = [
            v2("corrected", "2026-10-03T00:00:00Z", 99),
            v2("v2", "2026-10-04T00:00:00Z", 25),
            v2("ambiguous", "2026-10-05T00:00:00Z", 50, outcome="AMBIGUOUS"),
        ]
        self.assertEqual(
            confirmed_monthly_spend(legacy=history, v2_entries=rows, calendar_month="2026-10"),
            Decimal("55"),
        )

    def test_real_repository_round_trip_supplies_confirmed_v2_spend(self):
        with TemporaryDirectory() as root:
            repository = JsonAccountingRepository(Path(root))
            repository.post_once(v2("v2", "2026-10-31T23:59:59Z", 500))
            entries = repository.read_confirmed()
            self.assertEqual(len(entries), 1)
            self.assertEqual(
                confirmed_monthly_spend(legacy=[], v2_entries=entries, calendar_month="2026-10"),
                Decimal("500"),
            )
            self.assertEqual(
                confirmed_monthly_spend(legacy=[], v2_entries=entries, calendar_month="2026-11"),
                Decimal("0"),
            )


if __name__ == "__main__":
    unittest.main()
