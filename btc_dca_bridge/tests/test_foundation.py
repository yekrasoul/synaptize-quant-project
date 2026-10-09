import json
import unittest
from pathlib import Path
from decimal import Decimal

from btc_dca_bridge.config import load_strategy_config
from btc_dca_bridge.ledger import confirmed_executions, parse_executions
from btc_dca_bridge.portfolio import derive_portfolio


ROOT = Path(__file__).resolve().parents[1]


class FoundationContractsTest(unittest.TestCase):
    def test_schema_files_are_valid_json_schema_documents(self):
        schemas = sorted((ROOT / "schemas").glob("*.schema.json"))
        self.assertEqual(len(schemas), 22)
        for path in schemas:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertEqual(payload["type"], "object")
            self.assertFalse(payload.get("additionalProperties", True))

    def test_reconciled_ledger_is_parseable_and_totals_are_derived(self):
        from btc_dca_bridge.ledger import read_executions
        history = read_executions(ROOT / "ledger" / "executions.jsonl")
        active = confirmed_executions(history)
        strategy = load_strategy_config()
        september = derive_portfolio(history, "2026-09", strategy.monthly_cap_usd)
        october = derive_portfolio(history, "2026-10", strategy.monthly_cap_usd)
        self.assertEqual(len(history), 12)
        self.assertEqual(september.monthly_confirmed_usd_deployed, Decimal("220"))
        self.assertEqual(october.monthly_confirmed_usd_deployed, Decimal("95"))
        self.assertEqual(october.remaining_monthly_budget_usd, Decimal("405"))
        self.assertEqual(october.confirmed_execution_count, len(active))

    def test_projection_distinguishes_historical_events_from_active_executions(self):
        rows = [
            {"schema_version":"1.3.0","execution_id":"execution_original","executed_at_utc":"2026-10-01T10:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":25,"reference_price_usdt":80000,"btc_quantity":None,"status":"reconciled","reconciliation":{"source":"Project user-confirmed execution","note":"original","intake_interface":"project_chat"}},
            {"schema_version":"1.3.0","execution_id":"execution_corrected","executed_at_utc":"2026-10-01T10:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":30,"reference_price_usdt":81000,"btc_quantity":None,"status":"reconciled","reconciliation":{"source":"Project user-confirmed execution","note":"correction","intake_interface":"project_chat"},"supersedes_execution_id":"execution_original"},
            {"schema_version":"1.3.0","execution_id":"execution_voided","executed_at_utc":"2026-10-02T10:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":20,"reference_price_usdt":82000,"btc_quantity":None,"status":"reconciled","reconciliation":{"source":"Project user-confirmed execution","note":"other","intake_interface":"project_chat"}},
            {"schema_version":"1.3.0","execution_id":"execution_void","executed_at_utc":"2026-10-02T11:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":0,"reference_price_usdt":None,"btc_quantity":None,"status":"voided","reconciliation":{"source":"Project user-confirmed execution","note":"void","intake_interface":"project_chat"},"supersedes_execution_id":"execution_voided"},
        ]
        history = parse_executions("".join(json.dumps(row) + "\n" for row in rows))
        state = derive_portfolio(history, "2026-10", Decimal("500"))
        self.assertEqual(state.ledger_event_count, 4)
        self.assertEqual(state.derived_from_execution_ids, ("execution_corrected",))
        self.assertEqual(state.monthly_confirmed_usd_deployed, Decimal("30"))
        self.assertEqual(state.remaining_monthly_budget_usd, Decimal("470"))

    def test_v1_configuration_preserves_non_negotiable_values(self):
        config = (ROOT / "config" / "strategy_v1.yaml").read_text(encoding="utf-8")
        for required_fragment in (
            "primary_exchange: Bybit",
            "market_type: spot",
            "symbol: BTCUSDT",
            "approved_spot_source_order:",
            "- Bybit",
            "- Binance",
            "- KuCoin",
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
