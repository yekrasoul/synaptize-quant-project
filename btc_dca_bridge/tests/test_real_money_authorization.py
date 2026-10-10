import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType
from btc_dca_bridge.cli import main
from btc_dca_bridge.live_readiness import GateStatus, LiveExecutionGate
from btc_dca_bridge.real_money_authorization import (
    RealMoneyAuthorization,
    artifact_sha256,
    authorize_persisted_approval,
    operator_confirmation_digest,
    read_authorization_status,
)
from btc_dca_bridge.schemas import validate_artifact
from tests.test_live_readiness import LiveReadinessTests, NOW, SHA, safe_config


class RealMoneyAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = ArtifactStore(self.root)
        helper = LiveReadinessTests()
        self.approval = helper.approval(state="PREPARED")
        self.approval_receipt = self.approval.persist(self.store, run_id="run_20261010T173200Z_authprep1")
        self.confirmation = f"AUTHORIZE BYBIT SPOT BTCUSDT BUY $25 {self.approval.payload['client_order_id']}"

    def authorize(self, **changes):
        values = dict(
            store=self.store, approval_id=self.approval.approval_id,
            approval_sha256=self.approval_receipt.sha256,
            canary_manifest_id=self.approval.payload["canary_manifest_id"],
            canary_manifest_sha256=self.approval.payload["canary_manifest_sha256"],
            amount="25", client_order_id=self.approval.payload["client_order_id"],
            calendar_month="2026-10", confirmation=self.confirmation,
            run_id="run_20261010T173200Z_auth0001", now_utc=NOW,
        )
        values.update(changes)
        return authorize_persisted_approval(**values)

    def test_explicit_ceremony_creates_distinct_authorization_artifact(self):
        authorization, receipt = self.authorize()
        payload = authorization.to_dict()
        validate_artifact("real_money_authorization", payload)
        self.assertEqual(payload["status"], "AUTHORIZED")
        self.assertFalse(payload["standing_authorization"])
        self.assertFalse(payload["consumed"])
        self.assertTrue(payload["no_order_executed"])
        self.assertEqual(payload["operator_confirmation_digest"], operator_confirmation_digest(self.confirmation))
        self.assertNotIn(self.confirmation, json.dumps(payload))
        self.assertEqual(receipt.sha256, artifact_sha256(payload))

    def test_generic_confirmation_phrases_are_rejected(self):
        for confirmation in ("yes", "approve", "go", "بزن بریم", "confirm"):
            with self.subTest(confirmation=confirmation):
                with self.assertRaisesRegex(ValueError, "confirmation"):
                    self.authorize(confirmation=confirmation)

    def test_confirmation_amount_and_client_identity_must_match(self):
        with self.assertRaisesRegex(ValueError, "amount"):
            self.authorize(confirmation=f"AUTHORIZE BYBIT SPOT BTCUSDT BUY $35 {self.approval.payload['client_order_id']}")
        with self.assertRaisesRegex(ValueError, "client order"):
            self.authorize(confirmation="AUTHORIZE BYBIT SPOT BTCUSDT BUY $25 dca-" + "2" * 32)
        with self.assertRaisesRegex(ValueError, "amount"):
            self.authorize(amount="35")
        with self.assertRaisesRegex(ValueError, "client order"):
            self.authorize(client_order_id="dca-" + "2" * 32)

    def test_approval_sha_and_exact_bindings_are_verified(self):
        with self.assertRaisesRegex(ValueError, "SHA"):
            self.authorize(approval_sha256=SHA)
        with self.assertRaisesRegex(ValueError, "CanaryManifest"):
            self.authorize(canary_manifest_id="canary-other")
        with self.assertRaisesRegex(ValueError, "CanaryManifest"):
            self.authorize(canary_manifest_sha256="b" * 64)
        with self.assertRaisesRegex(ValueError, "month"):
            self.authorize(calendar_month="2026-11")

    def test_expired_or_non_prepared_phase54b_approval_blocks(self):
        helper = LiveReadinessTests()
        expired = helper.approval(state="PREPARED", ttl=timedelta(minutes=1))
        expired_payload = expired.to_dict()
        expired_store = ArtifactStore(self.root / "expired")
        receipt = expired.persist(expired_store, run_id="run_20261010T173200Z_auth0002")
        with self.assertRaisesRegex(ValueError, "expired"):
            authorize_persisted_approval(store=expired_store, approval_id=expired.approval_id, approval_sha256=receipt.sha256, canary_manifest_id=expired_payload["canary_manifest_id"], canary_manifest_sha256=expired_payload["canary_manifest_sha256"], amount="25", client_order_id=expired_payload["client_order_id"], calendar_month="2026-10", confirmation=f"AUTHORIZE BYBIT SPOT BTCUSDT BUY $25 {expired_payload['client_order_id']}", run_id="run_20261010T173200Z_auth03", now_utc=NOW + timedelta(minutes=2))
        blocked = helper.approval(state="BLOCKED")
        blocked_store = ArtifactStore(self.root / "blocked")
        blocked_receipt = blocked.persist(blocked_store, run_id="run_20261010T173200Z_auth0004")
        with self.assertRaisesRegex(ValueError, "PREPARED"):
            authorize_persisted_approval(store=blocked_store, approval_id=blocked.approval_id, approval_sha256=blocked_receipt.sha256, canary_manifest_id=blocked.payload["canary_manifest_id"], canary_manifest_sha256=blocked.payload["canary_manifest_sha256"], amount="25", client_order_id=blocked.payload["client_order_id"], calendar_month="2026-10", confirmation=f"AUTHORIZE BYBIT SPOT BTCUSDT BUY $25 {blocked.payload['client_order_id']}", run_id="run_20261010T173200Z_auth0005", now_utc=NOW)

    def test_exact_one_shot_identity_cannot_be_authorized_twice(self):
        self.authorize()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.authorize(run_id="run_20261010T173200Z_auth06")

    def test_existing_submission_evidence_blocks_ceremony(self):
        attempt = {
            "schema_version": "5.4.0B", "attempt_id": "attempt5b-" + "c" * 32,
            "created_at_utc": "2026-10-10T17:32:00Z", "approval_id": self.approval.approval_id,
            "approval_sha256": self.approval_receipt.sha256,
            "canary_manifest_id": self.approval.payload["canary_manifest_id"], "canary_manifest_sha256": SHA,
            "decision_id": self.approval.payload["decision_id"], "decision_sha256": SHA,
            "order_intent_id": self.approval.payload["order_intent_id"], "order_intent_sha256": SHA,
            "client_order_id": self.approval.payload["client_order_id"], "quote_amount_usdt": "25",
            "safe_request_fingerprint": SHA, "repository_commit_sha": "b" * 40,
            "account_identity_fingerprint": SHA, "calendar_month": "2026-10",
            "safety_gate_fingerprint": SHA, "reconciliation_fingerprint": SHA,
            "status": "PREPARED_FOR_SUBMISSION", "no_order_executed": True,
        }
        self.store.persist(ArtifactType.LIVE_EXECUTION_ATTEMPT, attempt, run_id="run_20261010T173200Z_auth0007")
        with self.assertRaisesRegex(ValueError, "submission evidence"):
            self.authorize(run_id="run_20261010T173200Z_auth0008")

    def test_verified_authorization_is_required_by_live_execution_gate(self):
        authorization, receipt = self.authorize()
        helper = LiveReadinessTests()
        current = helper.current(self.approval)
        current.update(safety_config=safe_config(), approval_sha256=self.approval_receipt.sha256, authorization_sha256=receipt.sha256, real_money_authorization=authorization.to_dict())
        result = LiveExecutionGate(self.approval, now=lambda: NOW).evaluate(current)
        self.assertEqual(result.status, GateStatus.READY_FOR_SUBMISSION)
        missing = dict(current, real_money_authorization=None)
        self.assertEqual(LiveExecutionGate(self.approval, now=lambda: NOW).evaluate(missing).status, GateStatus.BLOCKED)
        consumed = dict(current, real_money_authorization=dict(authorization.to_dict(), consumed=True))
        consumed_result = LiveExecutionGate(self.approval, now=lambda: NOW).evaluate(consumed)
        self.assertEqual(consumed_result.status, GateStatus.BLOCKED)

    def test_current_checked_in_safety_still_blocks_after_authorization(self):
        authorization, receipt = self.authorize()
        helper = LiveReadinessTests(); current = helper.current(self.approval)
        current.update(safety_config=safe_config(live_execution_enabled=False, kill_switch=True, order_submission="not_implemented"), approval_sha256=self.approval_receipt.sha256, authorization_sha256=receipt.sha256, real_money_authorization=authorization.to_dict())
        result = LiveExecutionGate(self.approval, now=lambda: NOW).evaluate(current)
        self.assertEqual(result.status, GateStatus.BLOCKED)

    def test_live_authorize_cli_persists_only_authorization_and_leaves_runtime_blocked(self):
        output = StringIO()
        args = ["live-authorize", "--approval-id", self.approval.approval_id, "--approval-sha", self.approval_receipt.sha256, "--canary-id", self.approval.payload["canary_manifest_id"], "--manifest-sha", self.approval.payload["canary_manifest_sha256"], "--amount", "25", "--client-order-id", self.approval.payload["client_order_id"], "--month", "2026-10", "--confirm", self.confirmation, "--run-id", "run_20261010T173200Z_auth0009", "--data-root", str(self.root)]
        with patch("btc_dca_bridge.cli.datetime") as clock, redirect_stdout(output):
            clock.now.return_value = NOW
            self.assertEqual(main(args), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["runtime"]["authorization"], "NOT_AUTHORIZED")
        self.assertEqual(result["runtime"]["live_execution_enabled"], False)
        self.assertEqual(result["runtime"]["kill_switch"], True)
        self.assertEqual(result["runtime"]["order_submission"], "not_implemented")

    def test_status_without_authorization_is_not_authorized_and_read_only(self):
        before = sorted(self.root.rglob("*"))
        result = read_authorization_status(store=self.store, now_utc=NOW)
        self.assertEqual(result, {
            "authorization": "NOT_AUTHORIZED", "execution": "BLOCKED",
            "live_execution_enabled": False, "kill_switch": True,
            "order_submission": "not_implemented", "message": "NO ORDER EXECUTED",
        })
        self.assertEqual(before, sorted(self.root.rglob("*")))

    def test_status_reports_valid_authorization_without_sensitive_material(self):
        authorization, _ = self.authorize()
        result = read_authorization_status(store=self.store, now_utc=NOW)
        self.assertEqual(result["authorization"], "AUTHORIZED")
        self.assertEqual(result["status"], "AUTHORIZED")
        self.assertEqual(result["approval_binding"], "VALID")
        self.assertEqual(result["authorization_id"], authorization.authorization_id)
        self.assertEqual(result["one_shot_identity"], authorization.one_shot_identity)
        rendered = json.dumps(result)
        self.assertNotIn("operator_confirmation_digest", rendered)
        self.assertNotIn("account_identity_fingerprint", rendered)
        self.assertNotIn(self.confirmation, rendered)
        self.assertNotIn("BYBIT_API_KEY", rendered)
        self.assertEqual(result["execution"], "BLOCKED")

    def test_status_derives_expired_authorization(self):
        self.authorize()
        result = read_authorization_status(store=self.store, now_utc=NOW + timedelta(minutes=11))
        self.assertEqual(result["status"], "EXPIRED")
        self.assertEqual(result["authorization"], "NOT_AUTHORIZED")
        self.assertEqual(result["execution"], "BLOCKED")

    def test_status_derives_consumed_from_submission_attempt(self):
        self.authorize()
        attempt = {
            "schema_version": "5.4.0B", "attempt_id": "attempt5b-" + "d" * 32,
            "created_at_utc": "2026-10-10T17:32:00Z", "approval_id": self.approval.approval_id,
            "approval_sha256": self.approval_receipt.sha256,
            "canary_manifest_id": self.approval.payload["canary_manifest_id"], "canary_manifest_sha256": SHA,
            "decision_id": self.approval.payload["decision_id"], "decision_sha256": SHA,
            "order_intent_id": self.approval.payload["order_intent_id"], "order_intent_sha256": SHA,
            "client_order_id": self.approval.payload["client_order_id"], "quote_amount_usdt": "25",
            "safe_request_fingerprint": SHA, "repository_commit_sha": "b" * 40,
            "account_identity_fingerprint": SHA, "calendar_month": "2026-10",
            "safety_gate_fingerprint": SHA, "reconciliation_fingerprint": SHA,
            "status": "PREPARED_FOR_SUBMISSION", "no_order_executed": True,
        }
        self.store.persist(ArtifactType.LIVE_EXECUTION_ATTEMPT, attempt, run_id="run_20261010T173200Z_auth0010")
        result = read_authorization_status(store=self.store, now_utc=NOW)
        self.assertEqual(result["status"], "CONSUMED")
        self.assertTrue(result["consumed"])
        self.assertEqual(result["authorization"], "NOT_AUTHORIZED")

    def test_corrupt_authorization_fails_closed(self):
        _, receipt = self.authorize()
        receipt.path.write_text("{malformed", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_authorization_status(store=self.store, now_utc=NOW)

    def test_status_cli_is_read_only_and_parser_accepts_command(self):
        ledger_before = sorted(self.root.rglob("*.json"))
        output = StringIO()
        with patch("http.client.HTTPSConnection") as connection, redirect_stdout(output):
            self.assertEqual(main(["live-authorization-status", "--data-root", str(self.root)]), 0)
        connection.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["authorization"], "NOT_AUTHORIZED")
        self.assertEqual(result["execution"], "BLOCKED")
        self.assertEqual(ledger_before, sorted(self.root.rglob("*.json")))
        self.assertNotIn("confirmation", output.getvalue().lower())
        self.assertNotIn("secret", output.getvalue().lower())


if __name__ == "__main__": unittest.main()
