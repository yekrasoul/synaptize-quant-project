import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from btc_dca_bridge import cli
from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType
from btc_dca_bridge.errors import ArtifactAlreadyExistsError
from btc_dca_bridge.errors import PersistenceIOError
from btc_dca_bridge.blocked_production import QUOTE_LIMIT_BLOCKER_ID, contract_status
from btc_dca_bridge.production_status import (
    ProductionStatusService,
    alert_class_for,
    compare_status_snapshots,
    derive_transition_alert,
    format_production_status_alert,
    validate_status_artifacts,
)


COMMIT = "a" * 40
CHECK_IDS = (
    "SECRET_HYGIENE", "BYBIT_CREDENTIAL_SCOPE", "BYBIT_ACCOUNT",
    "BYBIT_LIABILITIES", "BYBIT_SPOT_AVAILABLE_BALANCE", "CLOCK_SKEW",
    "PRODUCTION_CONNECTIVITY",
)


class MutableClock:
    def __init__(self, instant):
        self.instant = instant

    def __call__(self):
        return self.instant


class FakeStatusService:
    """Synthetic readiness/evidence/operations sources; no network clients."""
    def __init__(self, root, ledger, clock, *, unresolved=False, health=None, readiness_status="NOT_READY", checks=None):
        self.root, self.ledger, self.clock = root, ledger, clock
        self.unresolved = unresolved
        self.health_status = health or "HEALTHY_BLOCKED_EXTERNAL_DEPENDENCY"
        self.readiness_status = readiness_status
        self.checks = checks or {check: "PASS" for check in CHECK_IDS}
        self.service = ProductionStatusService(
            data_root=root,
            ledger_path=ledger,
            now=clock,
            readiness_factory=lambda **_: SimpleNamespace(evaluate=self.readiness),
            operations_factory=lambda **_: SimpleNamespace(health=self.health, snapshot=self.ops_snapshot),
            evidence_service_factory=lambda **_: SimpleNamespace(latest=self.latest, verify=self.verify),
            preauthorization_evaluator=lambda _: {"status": "BLOCKED"},
            contract_provider=lambda **kwargs: contract_status(now=kwargs["now"]),
            repo_probe=lambda: {"commit": COMMIT, "dirty": False},
            hostname=lambda: "test-host",
        )

    def readiness(self):
        return {
            "status": self.readiness_status,
            "checks": [
                {"check_id": check, "status": status, "required": True, "evidence": "sanitized", "reason": "synthetic"}
                for check, status in self.checks.items()
            ],
            "quote_unit_limit_evidence": {"conclusion": "NOT_EXPOSED", "authoritative": False, "maximum_quote_usdt": None},
        }

    def health(self):
        return {"status": self.health_status}

    def ops_snapshot(self):
        return SimpleNamespace(reconciliation_required=self.unresolved)

    def latest(self):
        return {"evidence_id": "evidence-test", "account_identity_status": "PROVEN"}

    def verify(self, _):
        return {"status": "VALID_NOT_READY"}


class ProductionStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "data"
        self.ledger = Path(self.temp.name) / "executions.jsonl"
        self.ledger.write_text("", encoding="utf-8")
        self.clock = MutableClock(datetime(2026, 10, 7, 12, tzinfo=UTC))
        self.fake = FakeStatusService(self.root, self.ledger, self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def test_status_reuses_preauthorization_verification_without_late_reverify(self):
        class Evidence:
            def __init__(self):
                self.verify_calls = 0

            def latest(self):
                return {
                    "evidence_id": "evidence-" + "a" * 32,
                    "account_identity_status": "PROVEN",
                }

            def verify(self, evidence_id):
                self.verify_calls += 1
                return {"status": "STALE", "evidence_id": evidence_id}

        evidence = Evidence()

        def evidence_factory(**kwargs):
            return evidence

        preauth = {
            "status": "BLOCKED",
            "verification": {
                "status": "VALID_NOT_READY",
                "evidence_id": "evidence-" + "a" * 32,
            },
        }

        service = ProductionStatusService(
            data_root=self.root,
            ledger_path=self.ledger,
            now=self.clock,
            readiness_factory=self.fake.service.readiness_factory,
            operations_factory=self.fake.service.operations_factory,
            evidence_service_factory=evidence_factory,
            preauthorization_evaluator=lambda _: preauth,
            contract_provider=self.fake.service.contract_provider,
            blocker_provider=self.fake.service.blocker_provider,
            repo_probe=self.fake.service.repo_probe,
            hostname=self.fake.service.hostname,
        )

        snapshot = service.evaluate().to_dict()

        self.assertEqual(snapshot["evidence_status"], "VALID_NOT_READY")
        self.assertEqual(snapshot["account_identity_status"], "PROVEN")
        self.assertEqual(snapshot["overall_operator_state"], "EXTERNAL_DEPENDENCY_BLOCKED")
        self.assertEqual(evidence.verify_calls, 0)

    def test_current_production_state_is_healthy_but_external_blocked(self):
        snapshot = self.fake.service.evaluate().to_dict()
        self.assertEqual(snapshot["software_health"], "HEALTHY_BLOCKED_EXTERNAL_DEPENDENCY")
        self.assertEqual(snapshot["production_readiness"], "NOT_READY")
        self.assertEqual(snapshot["preauthorization_status"], "BLOCKED")
        self.assertEqual(snapshot["blocker_ids"], [QUOTE_LIMIT_BLOCKER_ID])
        self.assertEqual(snapshot["quote_unit_limit_status"], "NOT_EXPOSED")
        self.assertEqual(snapshot["overall_operator_state"], "EXTERNAL_DEPENDENCY_BLOCKED")
        self.assertEqual(snapshot["canonical_execution_count"], 0)
        self.assertEqual(snapshot["ledger_event_count"], 0)
        self.assertEqual(snapshot["schema_version"], "6.1.0")
        self.assertEqual(snapshot["real_money_authorization"], {"granted": False, "source": "none", "required": True, "status": "NOT_AUTHORIZED"})

    def test_legacy_status_snapshot_schema_remains_readable(self):
        from btc_dca_bridge.schemas import validate_artifact
        current = self.fake.service.evaluate().to_dict()
        legacy = {key: value for key, value in current.items() if key != "ledger_event_count"}
        legacy["schema_version"] = "6.0.0"
        validate_artifact("production_status", legacy)
        ArtifactStore(self.root).persist(ArtifactType.PRODUCTION_STATUS, legacy, run_id=legacy["snapshot_id"])
        self.assertEqual(self.fake.service.history(limit=1)[0]["schema_version"], "6.0.0")

    def test_status_separates_history_events_from_active_executions(self):
        rows = [
            {"schema_version":"1.3.0","execution_id":"execution_a","executed_at_utc":"2026-10-01T10:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":25,"reference_price_usdt":80000,"btc_quantity":None,"status":"reconciled","reconciliation":{"source":"Project user-confirmed execution","note":"A","intake_interface":"project_chat"}},
            {"schema_version":"1.3.0","execution_id":"execution_b","executed_at_utc":"2026-10-01T10:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":30,"reference_price_usdt":81000,"btc_quantity":None,"status":"reconciled","reconciliation":{"source":"Project user-confirmed execution","note":"B","intake_interface":"project_chat"},"supersedes_execution_id":"execution_a"},
            {"schema_version":"1.3.0","execution_id":"execution_c","executed_at_utc":"2026-10-02T10:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":20,"reference_price_usdt":82000,"btc_quantity":None,"status":"reconciled","reconciliation":{"source":"Project user-confirmed execution","note":"C","intake_interface":"project_chat"}},
            {"schema_version":"1.3.0","execution_id":"execution_d","executed_at_utc":"2026-10-02T11:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":0,"reference_price_usdt":None,"btc_quantity":None,"status":"voided","reconciliation":{"source":"Project user-confirmed execution","note":"D","intake_interface":"project_chat"},"supersedes_execution_id":"execution_c"},
            {"schema_version":"1.3.0","execution_id":"execution_e","executed_at_utc":"2026-10-03T10:00:00Z","asset":"BTC","quote_currency":"USDT","executed_usd":10,"reference_price_usdt":100000,"btc_quantity":None,"status":"reconciled","reconciliation":{"source":"Project user-confirmed execution","note":"E","intake_interface":"codex_cli"}},
        ]
        self.ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        snapshot = self.fake.service.evaluate().to_dict()
        self.assertEqual(snapshot["ledger_event_count"], 5)
        self.assertEqual(snapshot["canonical_execution_count"], 2)

    def test_snapshot_persists_immutably_and_history_is_ordered(self):
        first = self.fake.service.collect()
        first_path = Path(first["snapshot_path"])
        self.assertTrue(first_path.exists())
        self.assertTrue(first_path.with_suffix(first_path.suffix + ".sha256").exists())
        original = first_path.read_bytes()
        with self.assertRaises(ArtifactAlreadyExistsError):
            ArtifactStore(self.root).persist(ArtifactType.PRODUCTION_STATUS, first["snapshot"], run_id=first["snapshot"]["snapshot_id"])
        self.assertEqual(first_path.read_bytes(), original)
        self.clock.instant += timedelta(seconds=1)
        second = self.fake.service.collect()
        rows = self.fake.service.history(limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["snapshot_id"], second["snapshot"]["snapshot_id"])
        self.assertEqual(rows[1]["snapshot_id"], first["snapshot"]["snapshot_id"])
        self.assertEqual(second["diff"]["classification"], "NO_MATERIAL_CHANGE")
        self.assertFalse(second["alert"]["created"])
        self.assertEqual(validate_status_artifacts(self.root)["status"], "valid")

    def _persist_material_snapshot_without_alert(self):
        first = self.fake.service.collect()
        self.assertEqual(first["new_alerts"], [])
        self.clock.instant += timedelta(seconds=1)
        self.fake.checks = dict(self.fake.checks, CLOCK_SKEW="UNAVAILABLE")
        current = self.fake.service.evaluate().to_dict()
        ArtifactStore(self.root).persist(ArtifactType.PRODUCTION_STATUS, current, run_id=current["snapshot_id"])
        return first, current

    def _assert_no_execution_side_effects(self, ledger_before):
        self.assertEqual(self.ledger.read_bytes(), ledger_before)
        self.assertFalse((self.root / "order_submission_attempts").exists())
        self.assertFalse((self.root / "live_approvals").exists())

    def test_crash_after_snapshot_persistence_backfills_exactly_once(self):
        ledger_before = self.ledger.read_bytes()
        self._persist_material_snapshot_without_alert()
        gap = validate_status_artifacts(self.root)
        self.assertEqual(gap["status"], "incomplete")
        self.assertEqual(gap["material_transition_count"], 1)
        self.assertEqual(gap["missing_alert_count"], 1)
        rows = self.fake.service.history(limit=10)
        expected = derive_transition_alert(rows[1], rows[1]["sha256"], rows[0], rows[0]["sha256"])
        self.assertIsNotNone(expected)
        self.assertEqual(expected, derive_transition_alert(rows[1], rows[1]["sha256"], rows[0], rows[0]["sha256"]))

        self.clock.instant += timedelta(seconds=1)
        recovered = self.fake.service.collect()
        self.assertEqual(recovered["reconciled_alert_count"], 1)
        self.assertEqual(len(recovered["new_alerts"]), 1)
        self.assertEqual(recovered["alert"]["classification"], "ATTENTION_REQUIRED")
        self.assertEqual(recovered["new_alerts"][0]["dedup_key"], expected["dedup_key"])
        self.assertEqual(recovered["diff"]["classification"], "NO_MATERIAL_CHANGE")
        self.assertEqual(validate_status_artifacts(self.root)["missing_alert_count"], 0)

        alert_count = validate_status_artifacts(self.root)["alert_count"]
        self.clock.instant += timedelta(seconds=1)
        repeated = self.fake.service.collect()
        self.assertEqual(repeated["new_alerts"], [])
        self.assertEqual(validate_status_artifacts(self.root)["alert_count"], alert_count)
        self._assert_no_execution_side_effects(ledger_before)

    def test_alert_persistence_failure_leaves_snapshot_for_later_recovery(self):
        ledger_before = self.ledger.read_bytes()
        self.fake.service.collect()
        self.clock.instant += timedelta(seconds=1)
        self.fake.checks = dict(self.fake.checks, CLOCK_SKEW="UNAVAILABLE")
        original_persist = self.fake.service.store.persist

        def fail_alert(kind, artifact, *, run_id):
            if kind is ArtifactType.PRODUCTION_STATUS_ALERT:
                raise OSError("synthetic alert persistence interruption")
            return original_persist(kind, artifact, run_id=run_id)

        with patch.object(self.fake.service.store, "persist", side_effect=fail_alert):
            with self.assertRaises(PersistenceIOError):
                self.fake.service.collect()
        self.assertEqual(len(self.fake.service.history(limit=10)), 2)
        self.assertEqual(validate_status_artifacts(self.root)["status"], "incomplete")
        self.assertFalse(list((self.root / "production_status_alerts").glob("*/*/*/*.json")))

        self.clock.instant += timedelta(seconds=1)
        recovered = self.fake.service.collect()
        self.assertEqual(recovered["reconciled_alert_count"], 1)
        self.assertEqual(validate_status_artifacts(self.root)["status"], "valid")
        self.assertEqual(validate_status_artifacts(self.root)["alert_count"], 1)
        self._assert_no_execution_side_effects(ledger_before)

    def test_validator_reports_gap_then_reconciliation_restores_coverage(self):
        ledger_before = self.ledger.read_bytes()
        self._persist_material_snapshot_without_alert()
        before = validate_status_artifacts(self.root)
        self.assertEqual(before["status"], "incomplete")
        self.assertEqual(before["missing_alert_count"], 1)
        reconciled = self.fake.service.reconcile_status_alerts()
        self.assertEqual(reconciled["created_count"], 1)
        after = validate_status_artifacts(self.root)
        self.assertEqual(after["status"], "valid")
        self.assertEqual(after["missing_alert_count"], 0)
        self._assert_no_execution_side_effects(ledger_before)

    def test_status_validator_distinguishes_corrupt_artifact_from_coverage_gap(self):
        collected = self.fake.service.collect()
        path = Path(collected["snapshot_path"])
        path.with_suffix(path.suffix + ".sha256").write_text("0" * 64 + "\n", encoding="ascii")
        report = validate_status_artifacts(self.root)
        self.assertEqual(report["status"], "CORRUPT")
        self.assertEqual(report["missing_alert_count"], 0)

    def test_telegram_failure_keeps_alert_and_is_not_retried_on_collection(self):
        ledger_before = self.ledger.read_bytes()
        self.fake.service.collect()
        self.clock.instant += timedelta(seconds=1)
        self.fake.checks = dict(self.fake.checks, CLOCK_SKEW="UNAVAILABLE")
        factory = lambda **_: self.fake.service
        notification = SimpleNamespace(
            bot_token_env_var="TEST_TELEGRAM_TOKEN",
            chat_id_env_var="TEST_TELEGRAM_CHAT",
            http=SimpleNamespace(connect_timeout_seconds=1, read_timeout_seconds=1, retry_attempts=1, backoff_seconds=0),
        )
        output = __import__("io").StringIO()
        with patch.object(cli, "_production_status_service_factory", side_effect=factory), \
             patch.object(cli, "load_notification_config", return_value=notification), \
             patch.object(cli, "TelegramNotifier") as notifier, \
             patch.dict("os.environ", {"TEST_TELEGRAM_TOKEN": "synthetic-token", "TEST_TELEGRAM_CHAT": "synthetic-chat"}), \
             patch("sys.stdout", output):
            notifier.return_value.send.side_effect = RuntimeError("synthetic delivery failure")
            result_code = cli.main(["collect-production-status", "--data-root", str(self.root), "--ledger", str(self.ledger), "--json"])
        result = json.loads(output.getvalue())
        self.assertEqual(result["notification_status"], "failed")
        self.assertEqual(result_code, 2)
        self.assertEqual(result["snapshot"]["production_readiness"], "NOT_READY")
        self.assertEqual(validate_status_artifacts(self.root)["alert_count"], 1)
        self.assertEqual(len(self.fake.service.history(limit=10)), 2)

        self.clock.instant += timedelta(seconds=1)
        output = __import__("io").StringIO()
        with patch.object(cli, "_production_status_service_factory", side_effect=factory), \
             patch.object(cli, "TelegramNotifier") as notifier_again, \
             patch("sys.stdout", output):
            cli.main(["collect-production-status", "--data-root", str(self.root), "--ledger", str(self.ledger), "--json"])
        repeated = json.loads(output.getvalue())
        self.assertEqual(repeated["new_alerts"], [])
        self.assertEqual(repeated["snapshot"]["production_readiness"], "NOT_READY")
        notifier_again.assert_not_called()
        self.assertEqual(validate_status_artifacts(self.root)["alert_count"], 1)
        self._assert_no_execution_side_effects(ledger_before)

    def test_status_diff_classifications(self):
        baseline = self.fake.service.evaluate().to_dict()
        self.assertEqual(compare_status_snapshots(baseline, dict(baseline)).classification, "NO_MATERIAL_CHANGE")
        external = dict(baseline, blocker_ids=[])
        self.assertEqual(compare_status_snapshots(baseline, external).classification, "EXTERNAL_DEPENDENCY_CHANGE")
        secret_failure = dict(baseline, secret_hygiene_status="FAIL")
        self.assertEqual(compare_status_snapshots(baseline, secret_failure).classification, "SAFETY_REGRESSION")
        identity_mismatch = dict(baseline, account_identity_status="MISMATCH")
        self.assertEqual(compare_status_snapshots(baseline, identity_mismatch).classification, "SAFETY_REGRESSION")
        unresolved = dict(baseline, unresolved_operations=True, overall_operator_state="RECONCILIATION_REQUIRED")
        self.assertEqual(compare_status_snapshots(baseline, unresolved).classification, "SAFETY_REGRESSION")
        recovered = dict(unresolved, unresolved_operations=False, overall_operator_state="EXTERNAL_DEPENDENCY_BLOCKED")
        self.assertEqual(compare_status_snapshots(unresolved, recovered).classification, "RECOVERY_PROGRESS")
        budget = dict(baseline, monthly_spent_usdt="10", remaining_monthly_budget_usdt="490")
        self.assertEqual(compare_status_snapshots(baseline, budget).classification, "INFO_CHANGE")

    def test_health_precedence_and_attention_states(self):
        unresolved_fake = FakeStatusService(self.root / "unresolved", self.ledger, self.clock, unresolved=True)
        self.assertEqual(unresolved_fake.service.evaluate().overall_operator_state, "RECONCILIATION_REQUIRED")
        corrupt = FakeStatusService(self.root / "corrupt", self.ledger, self.clock, health="CORRUPT")
        self.assertEqual(corrupt.service.evaluate().overall_operator_state, "CORRUPT")
        unsafe_checks = {check: "PASS" for check in CHECK_IDS}
        unsafe = FakeStatusService(self.root / "unsafe", self.ledger, self.clock, checks=unsafe_checks)
        with patch("btc_dca_bridge.production_status.load_execution_config", return_value=SimpleNamespace(live_execution_enabled=True, kill_switch=False, order_submission="implemented")):
            self.assertEqual(unsafe.service.evaluate().overall_operator_state, "ACTION_REQUIRED")
        stale_checks = {check: "PASS" for check in CHECK_IDS}
        stale_checks["CLOCK_SKEW"] = "UNAVAILABLE"
        stale = FakeStatusService(self.root / "stale", self.ledger, self.clock, checks=stale_checks)
        self.assertEqual(stale.service.evaluate().overall_operator_state, "ACTION_REQUIRED")

    def test_external_blocker_and_capability_change_never_authorize(self):
        baseline = self.fake.service.evaluate().to_dict()
        caps = dict(baseline["contract_capabilities"])
        caps["quote_unit_maximum_supported"] = True
        changed = dict(baseline, contract_capabilities=caps)
        self.assertEqual(compare_status_snapshots(baseline, changed).classification, "EXTERNAL_DEPENDENCY_CHANGE")
        self.assertEqual(changed["real_money_authorization"]["granted"], False)
        self.assertEqual(changed["production_readiness"], "NOT_READY")
        self.assertEqual(len(baseline["blocker_ids"]), 1)

    def test_alert_policy_deduplicates_material_change_and_formats_no_secrets(self):
        self.fake.service.collect()
        self.clock.instant += timedelta(seconds=1)
        changed_fake = FakeStatusService(self.root, self.ledger, self.clock, readiness_status="NOT_READY")
        changed_fake.checks = dict(changed_fake.checks, CLOCK_SKEW="UNAVAILABLE")
        changed = changed_fake.service.collect()
        self.assertTrue(changed["alert"]["created"])
        self.assertEqual(changed["alert"]["alert_class"], "WARNING")
        message = changed["alert"]["message"]
        self.assertIn("BTC DCA PRODUCTION STATUS", message)
        self.assertIn("Authorization: NOT_AUTHORIZED", message)
        self.assertNotIn("api_secret", message.lower())
        self.clock.instant += timedelta(seconds=1)
        repeated = changed_fake.service.collect()
        self.assertEqual(repeated["diff"]["classification"], "NO_MATERIAL_CHANGE")
        self.assertFalse(repeated["alert"]["created"])
        self.assertEqual(alert_class_for("SAFETY_REGRESSION"), "CRITICAL")
        self.assertIn("Observe only", format_production_status_alert(repeated["snapshot"], compare_status_snapshots(changed["snapshot"], repeated["snapshot"])))

    def test_status_cli_commands_have_no_execution_side_effects(self):
        ledger_before = self.ledger.read_bytes()
        transport_calls = []
        attempt_root = self.root / "order_submission_attempts"
        approval_root = self.root / "live_approvals"
        outputs = []
        with patch.object(cli, "_production_status_service_factory", side_effect=lambda **kw: FakeStatusService(kw["data_root"], kw.get("ledger_path", self.ledger), self.clock).service), patch.object(cli, "_submission_transport_factory", side_effect=lambda *args: transport_calls.append(args)):
            for argv in (
                ["production-status", "--data-root", str(self.root), "--ledger", str(self.ledger), "--json"],
                ["collect-production-status", "--data-root", str(self.root), "--ledger", str(self.ledger), "--json"],
                ["production-status-history", "--data-root", str(self.root), "--json"],
            ):
                stream = __import__("io").StringIO()
                with patch("sys.stdout", stream):
                    self.assertEqual(cli.main(argv), 0)
                outputs.append(json.loads(stream.getvalue()))
        self.assertEqual(transport_calls, [])
        self.assertEqual(self.ledger.read_bytes(), ledger_before)
        self.assertFalse(attempt_root.exists())
        self.assertFalse(approval_root.exists())
        self.assertEqual(outputs[0]["overall_operator_state"], "EXTERNAL_DEPENDENCY_BLOCKED")
        self.assertEqual(outputs[1]["snapshot"]["production_readiness"], "NOT_READY")
        self.assertEqual(outputs[2]["count"], 1)


if __name__ == "__main__":
    unittest.main()
