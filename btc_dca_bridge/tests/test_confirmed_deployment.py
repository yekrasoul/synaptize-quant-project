from __future__ import annotations

from decimal import Decimal
import unittest

from btc_dca_bridge.accounting.deployment import confirmed_monthly_spend
from btc_dca_bridge.models import Execution


def legacy(identity="legacy", when="2026-10-01T00:00:00Z", amount=25):
    return Execution({"execution_id": identity, "executed_at_utc": when, "executed_usd": amount, "status": "reconciled"})


class ConfirmedDeploymentTests(unittest.TestCase):
    def test_legacy_v2_month_dedup_and_unconfirmed_exclusion(self):
        rows = [
            {"execution_id":"v2", "occurred_at_utc":"2026-10-02T00:00:00Z", "quote_decrease":"30", "reconciliation_outcome":"CONFIRMED"},
            {"execution_id":"legacy", "occurred_at_utc":"2026-10-03T00:00:00Z", "quote_decrease":"99", "reconciliation_outcome":"CONFIRMED"},
            {"execution_id":"ambiguous", "occurred_at_utc":"2026-10-04T00:00:00Z", "quote_decrease":"50", "reconciliation_outcome":"AMBIGUOUS"},
            {"execution_id":"sep", "occurred_at_utc":"2026-09-30T00:00:00Z", "quote_decrease":"50", "reconciliation_outcome":"CONFIRMED"},
        ]
        self.assertEqual(confirmed_monthly_spend(legacy=[legacy()], v2_entries=rows, calendar_month="2026-10"), Decimal("55"))

    def test_v2_only_cap_and_month_boundary_are_deterministic(self):
        rows = [{"execution_id":"v2", "occurred_at_utc":"2026-10-31T23:59:59Z", "quote_decrease":"500", "reconciliation_outcome":"CONFIRMED"}, {"execution_id":"next", "occurred_at_utc":"2026-11-01T00:00:00Z", "quote_decrease":"1", "reconciliation_outcome":"CONFIRMED"}]
        self.assertEqual(confirmed_monthly_spend(legacy=[], v2_entries=rows, calendar_month="2026-10"), Decimal("500"))


if __name__ == "__main__": unittest.main()
