import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from btc_dca_bridge.config import load_strategy_config
from btc_dca_bridge.errors import LedgerValidationError, SchemaValidationError
from btc_dca_bridge.ledger import executions_for_month, read_executions
from btc_dca_bridge.paths import LEDGER_PATH
from btc_dca_bridge.portfolio import derive_portfolio
from btc_dca_bridge.schemas import validate_artifact


class LedgerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.executions = read_executions()

    def test_canonical_ledger_has_twelve_confirmed_executions(self):
        self.assertEqual(len(self.executions), 12)

    def test_canonical_ledger_total_is_315(self):
        total = sum((item.executed_usd for item in self.executions), Decimal(0))
        self.assertEqual(total, Decimal("315"))

    def test_september_confirmed_spend_is_220(self):
        rows = executions_for_month(self.executions, "2026-09")
        self.assertEqual(sum((row.executed_usd for row in rows), Decimal(0)), Decimal("220"))

    def test_october_confirmed_spend_is_95(self):
        rows = executions_for_month(self.executions, "2026-10")
        self.assertEqual(sum((row.executed_usd for row in rows), Decimal(0)), Decimal("95"))

    def test_calendar_month_filtering_excludes_other_months(self):
        rows = executions_for_month(self.executions, "2026-10")
        self.assertEqual([row.execution_id for row in rows], ["execution_20261005_01", "execution_20261008_01"])

    def test_rejects_invalid_calendar_month(self):
        for value in ("2026-00", "2026-13", "2026-1", "October"):
            with self.subTest(value=value), self.assertRaises(LedgerValidationError):
                executions_for_month(self.executions, value)

    def test_malformed_jsonl_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            path.write_text('{"execution_id":\n', encoding="utf-8")
            with self.assertRaisesRegex(LedgerValidationError, "malformed ledger JSON"):
                read_executions(path)

    def test_invalid_execution_record_is_rejected(self):
        row = json.loads(LEDGER_PATH.read_text(encoding="utf-8").splitlines()[0])
        row["executed_usd"] = 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(LedgerValidationError, "invalid execution"):
                read_executions(path)

    def test_duplicate_execution_id_is_rejected(self):
        line = LEDGER_PATH.read_text(encoding="utf-8").splitlines()[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            path.write_text(line + "\n" + line + "\n", encoding="utf-8")
            with self.assertRaisesRegex(LedgerValidationError, "duplicate execution_id"):
                read_executions(path)

    def test_reader_never_mutates_ledger(self):
        before = LEDGER_PATH.read_bytes()
        read_executions()
        self.assertEqual(LEDGER_PATH.read_bytes(), before)


class PortfolioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.strategy = load_strategy_config()
        cls.executions = read_executions()

    def test_october_portfolio_derivation(self):
        state = derive_portfolio(
            self.executions, "2026-10", self.strategy.monthly_cap_usd
        )
        self.assertEqual(state.total_confirmed_usd_deployed, Decimal("315"))
        self.assertEqual(state.monthly_confirmed_usd_deployed, Decimal("95"))
        self.assertEqual(state.remaining_monthly_budget_usd, Decimal("405"))
        self.assertEqual(state.confirmed_execution_count, 12)
        self.assertEqual(state.monthly_confirmed_execution_count, 2)
        self.assertEqual(state.schema_version, "1.1.0")
        self.assertEqual(state.as_of_utc, "2026-10-08T00:00:00Z")

    def test_derived_portfolio_is_a_schema_valid_v1_1_state(self):
        state = derive_portfolio(
            self.executions, "2026-10", self.strategy.monthly_cap_usd
        ).to_dict()
        validate_artifact("portfolio_state", state)
        self.assertEqual(state["monthly_spent_usd"], 95)
        self.assertEqual(state["monthly_remaining_usd"], 405)
        self.assertEqual(state["executions_count"], 12)

    def test_empty_portfolio_has_deterministic_as_of_timestamp(self):
        state = derive_portfolio((), "2026-10", self.strategy.monthly_cap_usd)
        self.assertEqual(state.as_of_utc, "2026-10-01T00:00:00Z")
        validate_artifact("portfolio_state", state.to_dict())

    def test_legacy_v1_portfolio_state_remains_schema_valid(self):
        validate_artifact(
            "portfolio_state",
            {
                "schema_version": "1.0.0",
                "as_of_utc": "2026-10-05T00:00:00Z",
                "calendar_month": "2026-10",
                "monthly_spent_usd": 60,
                "monthly_remaining_usd": 440,
                "executions_count": 11,
                "derived_from_execution_ids": ["execution_20261005_01"],
            },
        )

    def test_legacy_version_cannot_masquerade_as_evolved_state(self):
        state = derive_portfolio(
            self.executions, "2026-10", self.strategy.monthly_cap_usd
        ).to_dict()
        state["schema_version"] = "1.0.0"
        with self.assertRaisesRegex(
            SchemaValidationError, "portfolio_state schema violation"
        ):
            validate_artifact("portfolio_state", state)

    def test_nominal_btc_is_explicitly_reference_price_derived(self):
        state = derive_portfolio(
            self.executions, "2026-10", self.strategy.monthly_cap_usd
        ).to_dict()
        self.assertGreater(state["reference_price_derived_nominal_btc"], 0)
        self.assertNotIn("btc_quantity", state)
        self.assertIn("not actual exchange fill quantity", state["reference_price_quantity_disclaimer"])

    def test_weighted_reference_price_is_derived_from_nominal_btc(self):
        state = derive_portfolio(
            self.executions, "2026-10", self.strategy.monthly_cap_usd
        )
        expected = state.total_confirmed_usd_deployed / state.reference_price_derived_nominal_btc
        self.assertEqual(state.weighted_reference_acquisition_price_usdt, expected)


if __name__ == "__main__":
    unittest.main()
