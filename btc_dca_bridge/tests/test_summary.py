import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from btc_dca_bridge.cli import _summarize_shadow


def _outcome(source: str) -> dict[str, object]:
    return {
        "run_id": "run_test",
        "trigger_type": "manual",
        "logical_run_at_utc": "2026-10-07T11:00:00Z",
        "status": "completed",
        "notification_status": "sent",
        "shadow_result": {
            "market_snapshot": {
                "schema_version": "1.0.0",
                "source_exchange": "Bybit",
                "market_data_metadata": {"source": source},
            },
            "sentiment_snapshot": {"value": 50},
            "decision": {"drawdown_percent": 10, "final_purchase_usd": 25},
        },
    }


class SummaryFormatterTest(unittest.TestCase):
    def _summary(self, source: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            summary_path = Path(directory) / "summary.md"
            result_path.write_text(json.dumps(_outcome(source)), encoding="utf-8")
            _summarize_shadow(
                Namespace(
                    result_json=result_path,
                    summary_file=summary_path,
                    retention_status="success",
                )
            )
            return summary_path.read_text(encoding="utf-8")

    def test_tradingview_fallback_uses_canonical_market_source(self):
        summary = self._summary("tradingview")
        self.assertIn("- Market source: `tradingview`", summary)
        self.assertNotIn("Market source: `unknown`", summary)

    def test_bybit_direct_uses_canonical_market_source(self):
        summary = self._summary("bybit_api")
        self.assertIn("- Market source: `bybit_api`", summary)
        self.assertNotIn("Market source: `unknown`", summary)

    def test_current_v1_summary_fields_and_no_order_guarantee_remain(self):
        summary = self._summary("tradingview")
        for field in (
            "- Mode: `SHADOW — NO ORDER EXECUTED`",
            "- Final purchase: `$25`",
            "- Drawdown: `10%`",
            "- Fear & Greed: `50`",
        ):
            self.assertIn(field, summary)


if __name__ == "__main__":
    unittest.main()
