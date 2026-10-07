import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FoundationContractsTest(unittest.TestCase):
    def test_schema_files_are_valid_json_schema_documents(self):
        schemas = sorted((ROOT / "schemas").glob("*.schema.json"))
        self.assertEqual(len(schemas), 18)
        for path in schemas:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertEqual(payload["type"], "object")
            self.assertFalse(payload.get("additionalProperties", True))

    def test_reconciled_ledger_is_parseable_and_totals_are_derived(self):
        rows = [json.loads(line) for line in (ROOT / "ledger" / "executions.jsonl").read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len({row["execution_id"] for row in rows}), len(rows))
        self.assertTrue(all(row["status"] == "reconciled" for row in rows))
        september = sum(row["executed_usd"] for row in rows if row["executed_at_utc"].startswith("2026-09"))
        october = sum(row["executed_usd"] for row in rows if row["executed_at_utc"].startswith("2026-10"))
        self.assertEqual(september, 220)
        self.assertEqual(october, 60)

    def test_v1_configuration_preserves_non_negotiable_values(self):
        config = (ROOT / "config" / "strategy_v1.yaml").read_text(encoding="utf-8")
        for required_fragment in (
            "primary_exchange: Bybit",
            "market_type: spot",
            "symbol: BTCUSDT",
            "window_hours: 168",
            "condition: drawdown_percent >= -5",
            "condition: drawdown_percent < -25",
            "core_daily_usd: 10",
            "minimum_purchase_usd: 10",
            "monthly_cap_usd: 500",
            "rounding_tie_breaking: ROUND_HALF_UP",
            "live_order_submission: prohibited",
            "multiplier: 1.50",
            "multiplier: 0.50",
        ):
            self.assertIn(required_fragment, config)


if __name__ == "__main__":
    unittest.main()
