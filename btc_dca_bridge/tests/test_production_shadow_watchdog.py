import unittest
from pathlib import Path

import yaml

from btc_dca_bridge.monitoring.schedule import classify_production_shadow_freshness, production_shadow_policy
from btc_dca_bridge.monitoring.shadow_health import evaluate_evidence


ROOT = Path(__file__).resolve().parents[2]
WATCHDOG = ROOT / ".github/workflows/production-shadow-watchdog.yml"
SHADOW = ROOT / ".github/workflows/production-shadow.yml"


def run(identifier, created_at, *, status="completed", conclusion="success", event="schedule"):
    return {"id": identifier, "event": event, "head_branch": "main", "created_at": created_at, "status": status, "conclusion": conclusion}


class ProductionShadowScheduleHealthTests(unittest.TestCase):
    def test_delayed_scheduled_delivery_is_fresh_not_missing(self):
        result = classify_production_shadow_freshness("2026-10-23T22:21:26Z", [run(40027487013, "2026-10-23T21:32:13Z")])
        self.assertEqual(result["state"], "HEALTHY")
        self.assertEqual(result["run_id"], 40027487013)

    def test_late_watchdog_uses_freshness_not_nominal_slot(self):
        result = classify_production_shadow_freshness("2026-10-24T02:21:26Z", [run(1, "2026-10-23T21:32:13Z")])
        self.assertEqual(result["state"], "HEALTHY")

    def test_success_outside_freshness_slo_alerts(self):
        result = classify_production_shadow_freshness("2026-10-24T08:30:00Z", [run(1, "2026-10-23T21:32:13Z")])
        self.assertEqual(result["state"], "ALERT")

    def test_missing_manual_and_terminal_failures_fail_closed(self):
        self.assertEqual(classify_production_shadow_freshness("2026-01-02T07:23:00Z", [])["state"], "ALERT")
        self.assertEqual(classify_production_shadow_freshness("2026-01-02T07:23:00Z", [run(1, "2026-01-02T06:23:00Z", event="workflow_dispatch")])["state"], "ALERT")
        for conclusion in ("failure", "cancelled", "timed_out"):
            self.assertEqual(classify_production_shadow_freshness("2026-01-02T07:23:00Z", [run(1, "2026-01-02T06:23:00Z", conclusion=conclusion)])["state"], "ALERT")

    def test_running_malformed_and_rollover_evidence(self):
        self.assertEqual(classify_production_shadow_freshness("2026-01-02T07:23:00Z", [run(1, "2026-01-02T06:23:00Z", status="in_progress", conclusion=None)])["state"], "DEGRADED")
        self.assertEqual(classify_production_shadow_freshness("2026-01-02T00:10:00Z", [run(1, "2026-01-01T18:23:00Z")])["state"], "HEALTHY")
        self.assertEqual(classify_production_shadow_freshness("2026-01-02T07:23:00Z", [run(1, "bad")])["state"], "UNKNOWN")

    def test_monitor_execution_is_separate_from_observed_health(self):
        result = evaluate_evidence({"observed_at": "2026-01-02T07:23:00Z", "shadow_runs": []})
        self.assertEqual(result["monitor_execution_status"], "SUCCESS")
        self.assertEqual(result["scheduler_health_status"], "ALERT")
        self.assertEqual(result["system_health_status"], "ALERT")
        self.assertEqual(evaluate_evidence({})["monitor_execution_status"], "FAILED")

    def test_workflow_is_thin_bounded_and_schedule_policy_drifts_never(self):
        text = WATCHDOG.read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)
        triggers = workflow.get("on", workflow.get(True))
        self.assertEqual(triggers["schedule"], [{"cron": "23 1,7,13,19 * * *"}])
        self.assertIn("btc_dca_bridge.monitoring.shadow_health", text)
        self.assertIn("per_page: 20", text)
        self.assertIn("retries: 3", text)
        self.assertNotIn("productionSlot", text)
        self.assertNotIn("graceEnd", text)
        shadow = yaml.safe_load(SHADOW.read_text(encoding="utf-8"))
        shadow_triggers = shadow.get("on", shadow.get(True))
        self.assertEqual(shadow_triggers["schedule"][0]["cron"], production_shadow_policy().cron)
        self.assertEqual(shadow["jobs"]["shadow"]["timeout-minutes"], 30)


if __name__ == "__main__":
    unittest.main()
