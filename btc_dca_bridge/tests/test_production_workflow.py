import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "production-shadow.yml"


class ProductionWorkflowTest(unittest.TestCase):
    def test_workflow_yaml_parses(self):
        payload = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        self.assertIn("jobs", payload)

    def test_workflow_is_canonical_frozen_least_privilege_and_durable(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        lowered = workflow.lower()
        self.assertIn('cron: "0 11 * * *"', workflow)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("actions: read", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertIn("uv sync --frozen", workflow)
        self.assertIn("python -m btc_dca_bridge production-shadow", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("actions/download-artifact@v4", workflow)
        self.assertIn("retention-days: 90", workflow)
        self.assertIn("TELEGRAM_BOT_TOKEN", workflow)
        self.assertIn("TELEGRAM_CHAT_ID", workflow)
        for forbidden in (
            "binance",
            "kucoin",
            "place_order",
            "createorder",
            "private api",
            "bybit_api_key",
            "api_secret",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_workflow_never_hides_a_critical_step_failure(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("continue-on-error", workflow)
        self.assertIn("if: failure()", workflow)
        self.assertIn("DURABLE_RETENTION_FAILED", workflow)


if __name__ == "__main__":
    unittest.main()
