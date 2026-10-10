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
from btc_dca_bridge.config import load_execution_config
from btc_dca_bridge.live_activation import (
    ACTIVATION_TTL,
    ExecutionActivation,
    ExecutionCapability,
    activation_phrase_digest,
    create_execution_activation,
    gate_c,
    issue_execution_capability,
    read_activation_status,
)
from btc_dca_bridge.live_readiness import GateResult, GateStatus, LiveExecutionApproval
from btc_dca_bridge.real_money_authorization import RealMoneyAuthorization, artifact_sha256
from btc_dca_bridge.schemas import validate_artifact
from tests.test_live_readiness import NOW, SHA, safe_config


class LiveActivationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = ArtifactStore(self.root)
        self.approval = LiveExecutionApproval.prepare(
            now_utc=NOW, repository_commit_sha="b" * 40,
            strategy_id="btc_adaptive_dca_v1", strategy_version="1.0.0",
            canary_manifest_id="canary-1", canary_manifest_sha256=SHA,
            decision_id="decision-1", decision_sha256=SHA,
            order_intent_id="intent-1", order_intent_sha256=SHA,
            client_order_id="dca-" + "1" * 32, quote_amount_usdt="25",
            calendar_month="2026-10", monthly_spent_usdt="0", remaining_monthly_budget_usdt="500",
            account_identity_fingerprint=SHA, account_mode="spot",
            credential_scope_classification="SPOT_TRADE_NO_WITHDRAW", liability_state="NONE",
            spot_usdt_availability_fingerprint=SHA, instrument_metadata_fingerprint=SHA,
            clock_skew_evidence_fingerprint=SHA, reconciliation_evidence_fingerprint=SHA,
            production_contract_fingerprint=SHA, execution_safety_config_fingerprint=SHA,
            manifest_expires_at_utc="2026-10-10T17:42:00Z", state="PREPARED",
        )
        self.approval_receipt = self.approval.persist(self.store, run_id="run_20261010T173200Z_actprep1")
        self.auth = RealMoneyAuthorization.from_approval(
            self.approval, approval_sha256=self.approval_receipt.sha256,
            canary_manifest_id=self.approval.payload["canary_manifest_id"],
            canary_manifest_sha256=self.approval.payload["canary_manifest_sha256"],
            amount="25", client_order_id=self.approval.payload["client_order_id"],
            calendar_month="2026-10",
            confirmation=f"AUTHORIZE BYBIT SPOT BTCUSDT BUY $25 {self.approval.payload['client_order_id']}",
            now_utc=NOW,
        )
        self.auth_receipt = self.auth.persist(self.store, run_id="run_20261010T173200Z_actauth1")
        self.phrase = f"ACTIVATE ONE SHOT BYBIT SPOT BTCUSDT BUY $25 {self.approval.payload['client_order_id']}"

    def activate(self, **changes):
        values = dict(
            store=self.store, authorization_id=self.auth.authorization_id,
            authorization_sha256=self.auth_receipt.sha256,
            approval_id=self.approval.approval_id, approval_sha256=self.approval_receipt.sha256,
            client_order_id=self.approval.payload["client_order_id"], amount="25",
            calendar_month="2026-10", phrase=self.phrase,
            run_id="run_20261010T173200Z_act00001", now_utc=NOW,
        )
        values.update(changes)
        return create_execution_activation(**values)

    def test_activation_schema_determinism_and_ttl(self):
        activation, receipt = self.activate()
        validate_artifact("execution_activation", activation.to_dict())
        self.assertEqual(receipt.sha256, artifact_sha256(activation.to_dict()))
        regenerated = ExecutionActivation.from_authorization(
            self.auth.to_dict(), authorization_sha256=self.auth_receipt.sha256,
            approval=self.approval, approval_sha256=self.approval_receipt.sha256,
            phrase=self.phrase, now_utc=NOW,
        )
        self.assertEqual(activation.activation_id, regenerated.activation_id)
        created = datetime.fromisoformat(activation.payload["created_at_utc"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(activation.payload["expires_at_utc"].replace("Z", "+00:00"))
        self.assertLessEqual(expires - created, ACTIVATION_TTL)
        self.assertFalse(activation.payload["standing_activation"])
        self.assertTrue(activation.payload["no_order_executed"])

    def test_exact_phrase_and_identity_are_required(self):
        for phrase in ("yes", "go", "activate", "بزن بریم", "execute", "confirm"):
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError): self.activate(phrase=phrase)
        with self.assertRaisesRegex(ValueError, "amount"):
            self.activate(phrase=f"ACTIVATE ONE SHOT BYBIT SPOT BTCUSDT BUY $35 {self.approval.payload['client_order_id']}")
        with self.assertRaisesRegex(ValueError, "client"):
            self.activate(phrase="ACTIVATE ONE SHOT BYBIT SPOT BTCUSDT BUY $25 dca-" + "2" * 32)
        self.assertEqual(activation_phrase_digest(self.phrase, amount="25", client_order_id=self.approval.payload["client_order_id"]), self.activate()[0].payload["activation_nonce_digest"])

    def test_binding_mismatches_and_duplicate_activation_block(self):
        self.activate()
        with self.assertRaisesRegex(ValueError, "existing usable"):
            self.activate(run_id="run_20261010T173200Z_act00003")
        for key, value in (("authorization_id", "rmauth-" + "2" * 32), ("approval_id", "approval5b-" + "2" * 32), ("calendar_month", "2026-11"), ("client_order_id", "dca-" + "2" * 32)):
            with self.subTest(key=key):
                with self.assertRaises(ValueError): self.activate(**{key: value})

    def test_expired_activation_can_be_read_but_cannot_issue_capability(self):
        self.activate()
        result = read_activation_status(store=self.store, now_utc=NOW + timedelta(seconds=61))
        self.assertEqual(result["activation"], "EXPIRED")
        activation_payload, activation_sha = self.store.find_artifact(ArtifactType.EXECUTION_ACTIVATION, identity_field="activation_id", identity_value=result["activation_id"])
        ready = GateResult(GateStatus.READY_FOR_SUBMISSION, "A", (), SHA)
        with self.assertRaisesRegex(ValueError, "expired"):
            issue_execution_capability(activation=activation_payload, activation_sha256=activation_sha, gate_a=ready, gate_b=ready, request_fingerprint=SHA, runtime_config=safe_config(), now_utc=NOW + timedelta(seconds=61))

    def test_status_derives_consumption_from_submission_evidence_and_default_is_blocked(self):
        self.assertEqual(read_activation_status(store=ArtifactStore(self.root / "empty"))["activation"], "NOT_ACTIVATED")
        activation, _ = self.activate()
        before = sorted(self.root.rglob("*.json"))
        current = read_activation_status(store=self.store, now_utc=NOW)
        self.assertEqual(current["activation"], "ACTIVE")
        self.assertEqual(current["execution"], "BLOCKED")
        self.assertEqual(current["live_execution_enabled"], False)
        self.assertEqual(current["kill_switch"], True)
        self.assertEqual(current["order_submission"], "not_implemented")
        self.assertEqual(before, sorted(self.root.rglob("*.json")))
        attempt = {
            "schema_version": "5.4.0B", "attempt_id": "attempt5b-" + "e" * 32,
            "created_at_utc": "2026-10-10T17:32:00Z", "approval_id": self.approval.approval_id,
            "approval_sha256": self.approval_receipt.sha256, "canary_manifest_id": "canary-1", "canary_manifest_sha256": SHA,
            "decision_id": "decision-1", "decision_sha256": SHA, "order_intent_id": "intent-1", "order_intent_sha256": SHA,
            "client_order_id": self.auth.payload["client_order_id"], "quote_amount_usdt": "25", "safe_request_fingerprint": SHA,
            "repository_commit_sha": "b" * 40, "account_identity_fingerprint": SHA, "calendar_month": "2026-10",
            "safety_gate_fingerprint": SHA, "reconciliation_fingerprint": SHA, "status": "PREPARED_FOR_SUBMISSION", "no_order_executed": True,
        }
        self.store.persist(ArtifactType.LIVE_EXECUTION_ATTEMPT, attempt, run_id="run_20261010T173200Z_actattempt")
        consumed = read_activation_status(store=self.store, now_utc=NOW)
        self.assertEqual(consumed["activation"], "CONSUMED")
        self.assertTrue(consumed["consumed"])

    def test_kill_switch_and_gate_c_always_win(self):
        activation, receipt = self.activate()
        ready = GateResult(GateStatus.READY_FOR_SUBMISSION, "A", (), SHA)
        with self.assertRaisesRegex(ValueError, "kill switch"):
            issue_execution_capability(activation=activation.to_dict(), activation_sha256=receipt.sha256, gate_a=ready, gate_b=ready, request_fingerprint=SHA, runtime_config=safe_config(kill_switch=True), now_utc=NOW)
        capability = issue_execution_capability(activation=activation.to_dict(), activation_sha256=receipt.sha256, gate_a=ready, gate_b=ready, request_fingerprint=SHA, runtime_config=safe_config(), now_utc=NOW)
        with self.assertRaisesRegex(ValueError, "kill switch"):
            gate_c(capability=capability, activation=activation.to_dict(), activation_sha256=receipt.sha256, request_fingerprint=SHA, runtime_config=safe_config(kill_switch=True), now_utc=NOW)
        with self.assertRaises(ValueError):
            gate_c(capability=capability, activation=activation.to_dict(), activation_sha256=receipt.sha256, request_fingerprint="0" * 64, runtime_config=safe_config(), now_utc=NOW)

    def test_capability_is_ephemeral_and_single_use(self):
        activation, receipt = self.activate()
        ready = GateResult(GateStatus.READY_FOR_SUBMISSION, "A", (), SHA)
        capability = issue_execution_capability(activation=activation.to_dict(), activation_sha256=receipt.sha256, gate_a=ready, gate_b=ready, request_fingerprint=SHA, runtime_config=safe_config(), now_utc=NOW)
        gate_c(capability=capability, activation=activation.to_dict(), activation_sha256=receipt.sha256, request_fingerprint=SHA, runtime_config=safe_config(), now_utc=NOW)
        capability.consume()
        with self.assertRaisesRegex(ValueError, "consumed"):
            gate_c(capability=capability, activation=activation.to_dict(), activation_sha256=receipt.sha256, request_fingerprint=SHA, runtime_config=safe_config(), now_utc=NOW)

    def test_cli_status_commands_are_read_only_and_do_not_construct_network(self):
        output = StringIO()
        with patch("http.client.HTTPSConnection") as connection, redirect_stdout(output):
            self.assertEqual(main(["live-activation-status", "--data-root", str(self.root / "none")]), 0)
        connection.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["activation"], "NOT_ACTIVATED")
        self.assertEqual(result["execution"], "BLOCKED")
        self.assertNotIn("nonce", output.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
