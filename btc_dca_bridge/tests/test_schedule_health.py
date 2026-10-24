import unittest

from btc_dca_bridge.monitoring.schedule import classify_production_shadow_freshness
from btc_dca_bridge.monitoring.shadow_health import evaluate_evidence


def scheduled(run_id, created_at, *, status="completed", conclusion="success", event="schedule"):
    return {"id": run_id, "event": event, "head_branch": "main", "created_at": created_at, "status": status, "conclusion": conclusion}


class ScheduleHealthTests(unittest.TestCase):
    def test_delayed_scheduled_run_is_fresh_not_missing(self):
        result = classify_production_shadow_freshness("2026-10-23T22:21:26Z", [scheduled(40027487013, "2026-10-23T21:32:13Z")])
        self.assertEqual(result["state"], "HEALTHY")

    def test_missing_manual_terminal_and_malformed_evidence_fail_closed(self):
        observed = "2026-01-02T07:23:00Z"
        self.assertEqual(classify_production_shadow_freshness(observed, [])["state"], "ALERT")
        self.assertEqual(classify_production_shadow_freshness(observed, [scheduled(1, "2026-01-02T06:23:00Z", event="workflow_dispatch")])["state"], "ALERT")
        self.assertEqual(classify_production_shadow_freshness(observed, [scheduled(1, "bad")])["state"], "UNKNOWN")
        self.assertEqual(classify_production_shadow_freshness(observed, [scheduled(1, "2026-01-02T06:23:00Z", conclusion="timed_out")])["state"], "ALERT")

    def test_monitor_and_observed_health_are_distinct(self):
        result = evaluate_evidence({"observed_at": "2026-01-02T07:23:00Z", "shadow_runs": []})
        self.assertEqual(result["monitor_execution_status"], "SUCCESS")
        self.assertEqual(result["scheduler_health_status"], "ALERT")
        self.assertEqual(evaluate_evidence({})["monitor_execution_status"], "FAILED")
