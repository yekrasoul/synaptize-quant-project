import json
import tempfile
import unittest
from pathlib import Path

import yaml

from btc_dca_bridge.health_monitor import (
    CRITICAL_CHECKS,
    CRITICAL_CRON,
    FULL_CRON,
    authorization_from_canonical,
    canonical_contract_state,
    classify_artifact_integrity,
    classify_canonical_ci,
    classify_market_data_artifacts,
    classify_production_shadow,
    classify_production_status,
    classify_self_hosted_runner,
    commit_state,
    derive_mode,
    evaluate,
    expected_production_slot,
    render_summary,
    safety_violations,
)


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "system-health-monitor.yml"
SAFETY = {
    "live_execution_enabled": False,
    "kill_switch": True,
    "order_submission": "not_implemented",
    "authorization": "NOT_AUTHORIZED",
}


def healthy_checks():
    return {name: "HEALTHY" for name in CRITICAL_CHECKS}


class SystemHealthMonitorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.text)

    def test_workflow_cadence_permissions_runner_and_inputs(self):
        triggers = self.workflow.get("on", self.workflow.get(True))
        self.assertEqual(triggers["schedule"], [{"cron": CRITICAL_CRON}, {"cron": FULL_CRON}])
        self.assertEqual(self.workflow["permissions"], {"contents": "read", "actions": "read"})
        self.assertEqual(self.workflow["jobs"]["monitor"]["runs-on"], "ubuntu-24.04")
        inputs = triggers["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["mode"]["options"], ["critical", "full"])
        self.assertEqual(inputs["mode"]["default"], "full")
        self.assertTrue(all(inputs[name]["type"] == "boolean" for name in ("send_test_alert", "send_test_summary")))
        self.assertNotIn("continue-on-error", self.text)

    def test_github_evidence_uses_filesystem_transport(self):
        self.assertNotIn("GITHUB_EVIDENCE_JSON", self.text)
        self.assertNotIn("core.setOutput('evidence_json'", self.text)
        self.assertIn("writeFileSync('/tmp/system-health-github-evidence.json'", self.text)
        self.assertIn('read_text(encoding="utf-8")', self.text)
        self.assertIn('/tmp/system-health-github-evidence.json', self.text)

    def test_package_importing_evaluator_uses_uv_project_environment(self):
        evaluator = self.text.split("- name: Evaluate local read-only health", 1)[1].split("uv run python -m btc_dca_bridge.health_monitor --evaluate", 1)[0]
        self.assertIn('SEND_TEST_SUMMARY="$SEND_TEST_SUMMARY" uv run python - "$state_dir/input.json" <<\'PY\'', evaluator)
        self.assertIn("from btc_dca_bridge.health_monitor import", evaluator)
        self.assertNotIn('SEND_TEST_SUMMARY="$SEND_TEST_SUMMARY" python - "$state_dir/input.json" <<\'PY\'', evaluator)

    def test_large_synthetic_evidence_round_trips_without_environment_transport(self):
        evidence = {"artifacts": [{"name": "production-shadow-evidence", "payload": "x" * (2 * 1024 * 1024)}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system-health-github-evidence.json"
            path.write_text(json.dumps(evidence), encoding="utf-8")
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(loaded["artifacts"][0]["payload"]), 2 * 1024 * 1024)

    def test_schedule_and_manual_mode_selection_is_exact(self):
        self.assertEqual(derive_mode(event_name="schedule", schedule=CRITICAL_CRON, requested_mode=None), "critical")
        self.assertEqual(derive_mode(event_name="schedule", schedule=FULL_CRON, requested_mode=None), "full")
        self.assertEqual(derive_mode(event_name="workflow_dispatch", schedule=None, requested_mode="full"), "full")
        with self.assertRaises(ValueError):
            derive_mode(event_name="schedule", schedule="17 */6 * * *", requested_mode=None)

    def test_production_shadow_slot_calculation_and_rollover(self):
        self.assertEqual(expected_production_slot("2026-10-10T01:23:00Z").isoformat(), "2026-10-10T00:23:00+00:00")
        self.assertEqual(expected_production_slot("2026-10-10T07:23:00Z").isoformat(), "2026-10-10T06:23:00+00:00")
        self.assertEqual(expected_production_slot("2026-10-10T00:10:00Z").isoformat(), "2026-10-09T18:23:00+00:00")

    def test_production_shadow_matching_and_fail_closed_statuses(self):
        observed = "2026-10-10T07:23:00Z"
        success = {"id": 1, "event": "schedule", "head_branch": "main", "created_at": "2026-10-10T06:23:00Z", "status": "completed", "conclusion": "success"}
        self.assertEqual(classify_production_shadow(observed, [success])["state"], "HEALTHY")
        manual = {**success, "id": 2, "event": "workflow_dispatch"}
        self.assertEqual(classify_production_shadow(observed, [manual])["state"], "ALERT")
        self.assertEqual(classify_production_shadow(observed, [])["state"], "ALERT")
        outside = {**success, "id": 3, "created_at": "2026-10-10T07:24:00Z"}
        self.assertEqual(classify_production_shadow(observed, [outside])["state"], "ALERT")
        for status, conclusion in (("queued", None), ("in_progress", None), ("completed", "failure"), ("completed", "cancelled"), ("completed", "timed_out")):
            with self.subTest(status=status, conclusion=conclusion):
                run = {**success, "status": status, "conclusion": conclusion}
                self.assertEqual(classify_production_shadow(observed, [run])["state"], "ALERT")
        malformed = {**success, "created_at": "not-a-time"}
        self.assertEqual(classify_production_shadow(observed, [malformed])["state"], "UNKNOWN")

    def test_runner_requires_matching_shadow_run_job_evidence(self):
        shadow = {"state": "HEALTHY", "run_id": 101}
        self.assertEqual(classify_self_hosted_runner(shadow, {"101": [{"run_id": 101, "runner_name": "shadow-runner"}]}), "HEALTHY")
        self.assertEqual(classify_self_hosted_runner(shadow, {"101": [{"run_id": 101, "runner_name": ""}]}), "UNKNOWN")
        self.assertEqual(classify_self_hosted_runner(shadow, {"100": [{"run_id": 100, "runner_name": "shadow-runner"}]}), "UNKNOWN")
        self.assertEqual(classify_self_hosted_runner(shadow, {"101": [{"run_id": 100, "runner_name": "shadow-runner"}]}), "UNKNOWN")
        for state in ("ALERT", "UNKNOWN"):
            self.assertNotEqual(classify_self_hosted_runner({"state": state, "run_id": None}, {}), "HEALTHY")

    def test_critical_excludes_full_only_artifact_integrity(self):
        result = evaluate(mode="critical", checks=healthy_checks(), safety=SAFETY, observed_at="2026-10-10T17:17:00Z")
        self.assertEqual(result["overall"], "HEALTHY")
        self.assertNotIn("artifact_consistency", result["components"])
        full = evaluate(mode="full", checks={**healthy_checks(), "artifact_consistency": "HEALTHY"}, safety=SAFETY, observed_at="2026-10-10T17:17:00Z")
        self.assertIn("artifact_consistency", full["components"])

    def test_canonical_ci_missing_success_failure_and_malformed(self):
        self.assertEqual(classify_canonical_ci([]), "UNKNOWN")
        base = {"head_branch": "main", "created_at": "2026-10-10T06:00:00Z", "status": "completed"}
        self.assertEqual(classify_canonical_ci([{**base, "conclusion": "success"}]), "HEALTHY")
        for conclusion in ("failure", "cancelled", "timed_out"):
            self.assertEqual(classify_canonical_ci([{**base, "conclusion": conclusion}]), "ALERT")
        self.assertEqual(classify_canonical_ci([{**base, "conclusion": "success", "created_at": "bad"}]), "UNKNOWN")

    def test_contract_state_contains_material_watch_features_and_fingerprint(self):
        contract = {"production_quote_limit_conclusion": "QUOTE_UNIT_MAX_NOT_REQUIRED", "approved_quote_unit_limit_source_count": 0, "real_money_authorization": {"status": "NOT_AUTHORIZED"}, "capabilities": {"quote_unit_maximum_supported": False, "quote_unit_maximum_source": None, "spot_quote_availability_supported": True, "market_unit_quote_coin_supported": True}}
        state = canonical_contract_state(contract, [])
        self.assertEqual(state["state"], "HEALTHY")
        self.assertEqual(set(state["details"]), {"blocker_ids", "production_quote_limit_conclusion", "approved_quote_unit_limit_source_count", "quote_unit_maximum_supported", "quote_unit_maximum_source", "spot_quote_availability_supported", "market_unit_quote_coin_supported", "real_money_authorization_status"})
        changed = canonical_contract_state({**contract, "production_quote_limit_conclusion": "QUOTE_UNIT_MAX_CONFIRMED"}, [])
        self.assertNotEqual(state["fingerprint"], changed["fingerprint"])
        blocked = canonical_contract_state(contract, [{"blocker_id": "BLOCKER"}])
        self.assertEqual(blocked["state"], "ALERT")

    def test_contract_drift_is_a_single_transition_and_not_repeated(self):
        checks = {**healthy_checks(), "contract_state": {"fingerprint": "new"}}
        previous = {"contract_state": {"fingerprint": "old"}, "delivered_alerts": {}}
        first = evaluate(mode="full", checks={**checks, "external_dependencies": "HEALTHY"}, safety=SAFETY, observed_at="2026-10-10T17:17:00Z", previous=previous)
        self.assertEqual([item["component"] for item in first["pending_notifications"] if item["component"] == "external_dependencies"], ["external_dependencies"])
        delivered = commit_state(first, previous, alerts_delivered=True, summary_delivered=False)
        same = evaluate(mode="full", checks={**checks, "external_dependencies": "HEALTHY"}, safety=SAFETY, observed_at="2026-10-10T18:17:00Z", previous=delivered)
        self.assertEqual([item for item in same["pending_notifications"] if item["component"] == "external_dependencies"], [])

    def test_authorization_is_read_from_canonical_sources_and_deviation_is_critical(self):
        self.assertEqual(authorization_from_canonical({"real_money_authorization": {"status": "NOT_AUTHORIZED"}}), "NOT_AUTHORIZED")
        self.assertEqual(authorization_from_canonical({"real_money_authorization": {"status": "AUTHORIZED"}}), "AUTHORIZED")
        self.assertEqual(authorization_from_canonical({"real_money_authorization": {"status": "NOT_AUTHORIZED"}}, {"real_money_authorization": {"status": "AUTHORIZED"}}), "UNKNOWN")
        self.assertEqual(safety_violations({**SAFETY, "authorization": "AUTHORIZED"}), ("authorization",))
        result = evaluate(mode="critical", checks=healthy_checks(), safety={**SAFETY, "authorization": "AUTHORIZED"}, observed_at="2026-10-10T17:17:00Z")
        self.assertEqual(result["overall"], "CRITICAL")

    def test_all_safety_invariants_are_critical(self):
        for key, value in (("live_execution_enabled", True), ("kill_switch", False), ("order_submission", "implemented")):
            with self.subTest(key=key):
                result = evaluate(mode="critical", checks=healthy_checks(), safety={**SAFETY, key: value}, observed_at="2026-10-10T17:17:00Z")
                self.assertEqual(result["overall"], "CRITICAL")

    def test_artifact_presence_and_integrity_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "evidence.json"
            payload.write_text('{"captured_at_utc":"2026-10-10T16:00:00Z"}\n', encoding="utf-8")
            self.assertEqual(classify_artifact_integrity(root), "UNKNOWN")
            import hashlib
            (root / "evidence.json.sha256").write_text(hashlib.sha256(payload.read_bytes()).hexdigest() + "\n", encoding="utf-8")
            self.assertEqual(classify_artifact_integrity(root), "HEALTHY")
            payload.write_text('{"corrupt":true}\n', encoding="utf-8")
            self.assertEqual(classify_artifact_integrity(root), "ALERT")

    def test_market_data_requires_fresh_bybit_spot_btcusdt_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "snapshot.json"
            payload = {"captured_at_utc": "2026-10-10T16:30:00Z", "source_exchange": "Bybit", "market_type": "spot", "symbol": "BTCUSDT", "fresh": True}
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(classify_market_data_artifacts(root, "2026-10-10T17:00:00Z"), "HEALTHY")
            payload["source_exchange"] = "Other"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(classify_market_data_artifacts(root, "2026-10-10T17:00:00Z"), "UNKNOWN")

    def test_structured_production_status_is_not_healthy_on_action_required(self):
        self.assertEqual(classify_production_status(None), "UNKNOWN")
        payload = {"canonical_ledger_status": "VALID", "software_health": "HEALTHY", "overall_operator_state": "NO_ACTIVE_EXTERNAL_BLOCKERS"}
        self.assertEqual(classify_production_status(payload), "HEALTHY")
        self.assertEqual(classify_production_status({**payload, "overall_operator_state": "ACTION_REQUIRED"}), "DEGRADED")
        self.assertEqual(classify_production_status({**payload, "canonical_ledger_status": "CORRUPT"}), "ALERT")

    def test_full_mode_includes_critical_checks_and_critical_skips_bybit(self):
        critical = evaluate(mode="critical", checks=healthy_checks(), safety=SAFETY, observed_at="2026-10-10T17:17:00Z")
        self.assertNotIn("bybit_spot_connectivity", critical["components"])
        full = evaluate(mode="full", checks={**healthy_checks(), "bybit_spot_connectivity": "UNKNOWN"}, safety=SAFETY, observed_at="2026-10-10T18:43:00Z")
        self.assertIn("bybit_spot_connectivity", full["components"])
        self.assertEqual(full["overall"], "DEGRADED")

    def test_delivery_acknowledgement_controls_alert_retries_recovery_and_summary(self):
        failed_checks = {**healthy_checks(), "production_shadow": "ALERT"}
        first = evaluate(mode="critical", checks=failed_checks, safety=SAFETY, observed_at="2026-10-10T17:17:00Z")
        undelivered = commit_state(first, None, alerts_delivered=False, summary_delivered=False)
        retry = evaluate(mode="critical", checks=failed_checks, safety=SAFETY, observed_at="2026-10-10T18:17:00Z", previous=undelivered)
        self.assertTrue(any(item["component"] == "production_shadow" for item in retry["pending_notifications"]))
        delivered = commit_state(first, None, alerts_delivered=True, summary_delivered=True)
        suppressed = evaluate(mode="critical", checks=failed_checks, safety=SAFETY, observed_at="2026-10-10T19:17:00Z", previous=delivered)
        self.assertFalse(any(item["component"] == "production_shadow" for item in suppressed["pending_notifications"]))
        recovered = evaluate(mode="critical", checks=healthy_checks(), safety=SAFETY, observed_at="2026-10-10T20:17:00Z", previous=delivered)
        self.assertEqual([item["kind"] for item in recovered["pending_notifications"] if item["component"] == "production_shadow"], ["RECOVERED"])
        recovery_failed = commit_state(recovered, delivered, alerts_delivered=False, summary_delivered=False)
        recovered_retry = evaluate(mode="critical", checks=healthy_checks(), safety=SAFETY, observed_at="2026-10-10T21:17:00Z", previous=recovery_failed)
        self.assertEqual([item["kind"] for item in recovered_retry["pending_notifications"] if item["component"] == "production_shadow"], ["RECOVERED"])

    def test_daily_summary_and_manual_tests_do_not_advance_state_without_delivery(self):
        first = evaluate(mode="critical", checks=healthy_checks(), safety=SAFETY, observed_at="2026-10-10T17:17:00Z")
        self.assertTrue(first["daily_summary_due"])
        not_sent = commit_state(first, None, alerts_delivered=False, summary_delivered=False)
        retry = evaluate(mode="critical", checks=healthy_checks(), safety=SAFETY, observed_at="2026-10-10T18:17:00Z", previous=not_sent)
        self.assertTrue(retry["daily_summary_due"])
        sent = commit_state(first, None, alerts_delivered=False, summary_delivered=True)
        same_day = evaluate(mode="critical", checks=healthy_checks(), safety=SAFETY, observed_at="2026-10-10T19:17:00Z", previous=sent)
        self.assertFalse(same_day["daily_summary_due"])
        test = evaluate(mode="critical", checks=healthy_checks(), safety=SAFETY, observed_at="2026-10-10T20:17:00Z", previous=sent, send_test_alert=True, send_test_summary=True)
        self.assertEqual(test["components"], sent["components"])
        self.assertEqual(test["safety"], sent["safety"])
        self.assertTrue(test["daily_summary_due"])

    def test_summary_contains_required_fields_and_no_order(self):
        result = evaluate(mode="critical", checks=healthy_checks(), safety=SAFETY, observed_at="2026-10-10T17:17:00Z")
        summary = render_summary(result)
        for text in ("BTC DCA SYSTEM HEALTH", "Critical Monitor:", "Ledger Integrity:", "Authorization: NOT_AUTHORIZED", "NO ORDER EXECUTED"):
            self.assertIn(text, summary)

    def test_workflow_preserves_read_only_guards_and_old_monitors_remain(self):
        for forbidden in ("place_order", "createorder", "ledger-record", "canary-execute", "live_submit"):
            self.assertNotIn(forbidden, self.text.lower())
        self.assertIn("actions: read", self.text)
        self.assertTrue((ROOT / ".github/workflows/production-shadow-watchdog.yml").is_file())
        self.assertTrue((ROOT / ".github/workflows/bybit-contract-watch.yml").is_file())


if __name__ == "__main__":
    unittest.main()
