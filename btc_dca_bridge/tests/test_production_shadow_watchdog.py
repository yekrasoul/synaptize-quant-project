import json
import subprocess
import unittest
from pathlib import Path

import yaml


APP_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = APP_ROOT.parent
WATCHDOG_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "production-shadow-watchdog.yml"
SHADOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "production-shadow.yml"


def workflow_triggers(workflow):
    return workflow.get("on", workflow.get(True))


class ProductionShadowWatchdogWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WATCHDOG_PATH.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.text)

    def test_workflow_yaml_parses_and_has_required_identity(self):
        self.assertTrue(WATCHDOG_PATH.is_file())
        self.assertIsInstance(self.workflow, dict)
        self.assertEqual(self.workflow["name"], "Production shadow watchdog")
        self.assertIn("jobs", self.workflow)

    def test_schedule_dispatch_runner_permissions_and_concurrency(self):
        triggers = workflow_triggers(self.workflow)
        self.assertEqual(triggers["schedule"], [{"cron": "23 1,7,13,19 * * *"}])
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(self.workflow["permissions"], {"contents": "read", "actions": "read"})
        self.assertEqual(self.workflow["concurrency"], {
            "group": "production-shadow-watchdog",
            "cancel-in-progress": False,
        })
        job = self.workflow["jobs"]["watchdog"]
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertNotIn("self-hosted", self.text.lower())
        self.assertNotIn("btc-dca", self.text.lower())

    def test_monitors_only_scheduled_main_production_shadow_runs(self):
        self.assertIn("workflow_id: 'production-shadow.yml'", self.text)
        self.assertIn("run.event === 'schedule'", self.text)
        self.assertIn("run.head_branch === 'main'", self.text)
        self.assertIn("run.created_at", self.text)
        self.assertIn("graceEnd", self.text)
        self.assertNotIn("workflow_dispatch'", self.text)

    def run_monitor(self, watchdog_started, runs):
        """Run the workflow's embedded monitor script with deterministic API stubs."""
        monitor = next(
            step for step in self.workflow["jobs"]["watchdog"]["steps"]
            if step.get("id") == "monitor"
        )
        script = monitor["with"]["script"]
        harness = f"""
const outputs = {{}};
const failures = [];
const github = {{
  rest: {{
    actions: {{
      getWorkflowRun: async () => ({{data: {{run_started_at: {json.dumps(watchdog_started)}}}}}),
      listWorkflowRuns: async () => ({{data: {{workflow_runs: {json.dumps(runs)}}}}}),
    }},
  }},
  paginate: async () => {json.dumps(runs)},
}};
const context = {{repo: {{owner: "example", repo: "repo"}}, runId: 999}};
const core = {{
  setOutput: (name, value) => {{ outputs[name] = value; }},
  setFailed: (reason) => failures.push(reason),
}};
(async () => {{
{script}
  process.stdout.write(JSON.stringify({{outputs, failures}}));
}})().catch(error => {{
  process.stderr.write(String(error.stack || error));
  process.exit(1);
}});
"""
        completed = subprocess.run(
            ["node", "-"],
            input=harness,
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(completed.stdout)

    @staticmethod
    def scheduled_run(run_id, created_at, *, status="completed", conclusion="success"):
        return {
            "id": run_id,
            "event": "schedule",
            "head_branch": "main",
            "created_at": created_at,
            "status": status,
            "conclusion": conclusion,
        }

    def test_watchdog_0123_resolves_same_day_0023_slot(self):
        result = self.run_monitor("2026-01-02T01:23:00Z", [])
        self.assertEqual(result["outputs"]["expected_slot"], "2026-01-02T00:23:00.000Z")

    def test_watchdog_0723_resolves_same_day_0623_slot(self):
        result = self.run_monitor("2026-01-02T07:23:00Z", [])
        self.assertEqual(result["outputs"]["expected_slot"], "2026-01-02T06:23:00.000Z")

    def test_watchdog_before_first_slot_rolls_back_to_preceding_day_1823(self):
        result = self.run_monitor("2026-01-02T00:10:00Z", [])
        self.assertEqual(result["outputs"]["expected_slot"], "2026-01-01T18:23:00.000Z")

    def test_manual_dispatch_never_satisfies_health(self):
        result = self.run_monitor(
            "2026-01-02T07:23:00Z",
            [{
                "id": 10,
                "event": "workflow_dispatch",
                "head_branch": "main",
                "created_at": "2026-01-02T06:23:00Z",
                "status": "completed",
                "conclusion": "success",
            }],
        )
        self.assertEqual(result["outputs"]["watchdog_result"], "ALERT")
        self.assertEqual(result["outputs"]["matched_run_id"], "none")

    def test_scheduled_main_success_is_healthy(self):
        result = self.run_monitor(
            "2026-01-02T07:23:00Z",
            [self.scheduled_run(11, "2026-01-02T06:23:00Z")],
        )
        self.assertEqual(result["outputs"]["watchdog_result"], "HEALTHY")
        self.assertEqual(result["outputs"]["matched_run_id"], "11")

    def test_missing_run_is_alert(self):
        result = self.run_monitor("2026-01-02T07:23:00Z", [])
        self.assertEqual(result["outputs"]["watchdog_result"], "ALERT")
        self.assertEqual(result["outputs"]["status"], "missing")

    def test_queued_and_in_progress_runs_are_alerts(self):
        for status in ("queued", "in_progress"):
            with self.subTest(status=status):
                result = self.run_monitor(
                    "2026-01-02T07:23:00Z",
                    [self.scheduled_run(20, "2026-01-02T06:23:00Z", status=status, conclusion=None)],
                )
                self.assertEqual(result["outputs"]["watchdog_result"], "ALERT")
                self.assertEqual(result["outputs"]["status"], status)

    def test_failed_cancelled_and_timed_out_runs_are_alerts(self):
        for conclusion in ("failure", "cancelled", "timed_out"):
            with self.subTest(conclusion=conclusion):
                result = self.run_monitor(
                    "2026-01-02T07:23:00Z",
                    [self.scheduled_run(30, "2026-01-02T06:23:00Z", conclusion=conclusion)],
                )
                self.assertEqual(result["outputs"]["watchdog_result"], "ALERT")
                self.assertEqual(result["outputs"]["conclusion"], conclusion)

    def test_scheduled_run_outside_one_hour_window_does_not_match(self):
        result = self.run_monitor(
            "2026-01-02T07:23:00Z",
            [self.scheduled_run(40, "2026-01-02T07:24:00Z")],
        )
        self.assertEqual(result["outputs"]["watchdog_result"], "ALERT")
        self.assertEqual(result["outputs"]["matched_run_id"], "none")

    def test_telegram_is_alert_only_and_validates_response(self):
        telegram = next(
            step for step in self.workflow["jobs"]["watchdog"]["steps"]
            if step.get("name") == "Send Telegram alert"
        )
        self.assertEqual(
            telegram["if"],
            "always() && steps.monitor.outputs.watchdog_result == 'ALERT'",
        )
        self.assertIn("TELEGRAM_BOT_TOKEN", telegram["env"])
        self.assertIn("TELEGRAM_CHAT_ID", telegram["env"])
        self.assertIn("response.status != 200", self.text)
        self.assertIn("body.get(\"ok\") is not True", self.text)
        self.assertIn("timeout=10", self.text)
        self.assertIn("NO ORDER EXECUTED", self.text)
        self.assertIn("Authorization: NOT_AUTHORIZED", self.text)

    def test_summary_is_always_written_and_failures_are_not_hidden(self):
        summary = next(
            step for step in self.workflow["jobs"]["watchdog"]["steps"]
            if step.get("name") == "Write watchdog summary"
        )
        self.assertEqual(summary["if"], "always()")
        self.assertIn("expected slot UTC", summary["run"])
        self.assertIn("matched run ID", summary["run"])
        self.assertIn("watchdog result", summary["run"])
        self.assertIn("read-only: yes", summary["run"])
        self.assertNotIn("continue-on-error", self.text)

    def test_watchdog_has_no_production_execution_or_external_market_path(self):
        lowered = self.text.lower()
        for forbidden in (
            "bybit",
            "ledger",
            "live_execution_enabled",
            "kill_switch",
            "place_order",
            "createorder",
            "btc_dca_bridge",
            "production-shadow --",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_existing_production_shadow_workflow_remains_canonical(self):
        shadow = yaml.safe_load(SHADOW_PATH.read_text(encoding="utf-8"))
        triggers = workflow_triggers(shadow)
        self.assertEqual(shadow["name"], "Production shadow")
        self.assertEqual(triggers["schedule"], [{"cron": "23 */6 * * *"}])
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(shadow["jobs"]["shadow"]["runs-on"], ["self-hosted", "Linux", "X64", "btc-dca"])
        self.assertIn("python -m btc_dca_bridge production-shadow", SHADOW_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
