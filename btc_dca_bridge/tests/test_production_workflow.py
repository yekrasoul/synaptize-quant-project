import unittest
from pathlib import Path

import yaml

from btc_dca_bridge.config import load_persistence_config, load_runtime_config


APP_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = APP_ROOT.parent
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "production-shadow.yml"
NESTED_WORKFLOW_PATH = APP_ROOT / ".github" / "workflows" / "production-shadow.yml"


class ProductionWorkflowTest(unittest.TestCase):
    def test_workflow_yaml_parses(self):
        self.assertTrue(WORKFLOW_PATH.is_file())
        self.assertFalse(NESTED_WORKFLOW_PATH.exists())
        payload = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        self.assertIn("jobs", payload)

    def test_workflow_initializes_runtime_paths_after_runner_allocation(self):
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        job = workflow["jobs"]["shadow"]
        job_env = job.get("env", {})
        self.assertNotIn("CONTEXT_JSON", job_env)
        self.assertNotIn("RESULT_JSON", job_env)
        self.assertNotRegex(WORKFLOW_PATH.read_text(encoding="utf-8"), r"\$\{\{\s*runner\.")

        steps = job["steps"]
        path_step = next(step for step in steps if step.get("name") == "Initialize runner runtime paths")
        self.assertIn("RUNNER_TEMP", path_step["run"])
        self.assertIn('echo "CONTEXT_JSON=${RUNNER_TEMP}/production-context.json" >> "$GITHUB_ENV"', path_step["run"])
        self.assertIn('echo "RESULT_JSON=${RUNNER_TEMP}/production-result.json" >> "$GITHUB_ENV"', path_step["run"])
        self.assertLess(steps.index(path_step), next(i for i, step in enumerate(steps) if step.get("id") == "context"))

    def test_workflow_preserves_canonical_schedule_dispatch_and_artifact_paths(self):
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        triggers = workflow.get("on", workflow.get(True))
        self.assertEqual(workflow["name"], "Production shadow")
        self.assertEqual(triggers["schedule"][0]["cron"], "23 */6 * * *")
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(workflow["jobs"]["shadow"]["defaults"]["run"]["working-directory"], "btc_dca_bridge")
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("path: btc_dca_bridge/data/", text)
        self.assertIn('"${CONTEXT_JSON}"', text)
        self.assertIn('"${RESULT_JSON}"', text)

    def test_workflow_is_canonical_frozen_least_privilege_and_durable(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        lowered = workflow.lower()
        runtime = load_runtime_config()
        persistence = load_persistence_config()
        self.assertIn(f'cron: "{runtime.github_cron_utc}"', workflow)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("actions: read", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertIn("uv sync --frozen", workflow)
        self.assertIn("working-directory: btc_dca_bridge", workflow)
        self.assertIn("python -m btc_dca_bridge production-shadow", workflow)
        self.assertIn("actions/checkout@v5", workflow)
        self.assertIn("actions/github-script@v8", workflow)
        self.assertNotIn("actions/setup-python@v6", workflow)
        self.assertIn("uv sync --frozen --python /usr/bin/python3", workflow)
        self.assertIn("actions/upload-artifact@v6", workflow)
        self.assertIn("actions/download-artifact@v5", workflow)
        self.assertIn("astral-sh/setup-uv@v6", workflow)
        self.assertIn("runs-on: [self-hosted, Linux, X64, btc-dca]", workflow)
        self.assertIn("path: btc_dca_bridge/data", workflow)
        self.assertIn("retention-days: ${{ steps.context.outputs.completed_retention_days }}", workflow)
        self.assertIn("partial_retention_days", workflow)
        self.assertEqual(persistence.completed_retention_days, 90)
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
