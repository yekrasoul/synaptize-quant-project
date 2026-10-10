import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from btc_dca_bridge.artifacts import ArtifactStore
from btc_dca_bridge.cli import main
from btc_dca_bridge.config import ExecutionConfig, load_execution_config
from btc_dca_bridge.live_readiness import LiveExecutionApproval, ORDER_CONTRACT
from btc_dca_bridge.real_money_authorization import RealMoneyAuthorization, artifact_sha256
from btc_dca_bridge.runtime_execution import EphemeralExecutionPolicy, derive_ephemeral_execution_policy


class RuntimeExecutionTests(unittest.TestCase):
    def ceremony(self):
        now = datetime.now(UTC).replace(microsecond=0)
        digest = "a" * 64
        commit = "b" * 40
        client_order_id = "dca-" + "c" * 32
        intent = {
            "schema_version": "5.4.0",
            "strategy_id": "btc_adaptive_dca_v1",
            "strategy_version": "1.0.0",
            "decision_id": "decision-1",
            "order_intent_id": "intent-1",
            "client_order_id": client_order_id,
            "quote_amount_usd": "25",
            "calendar_month": "2026-10",
        }
        decision = {"decision_id": "decision-1", "strategy_id": "btc_adaptive_dca_v1"}
        manifest = {"canary_id": "canary-1", "decision_id": "decision-1", "order_intent_id": "intent-1", "strategy_id": "btc_adaptive_dca_v1"}
        intent_sha, decision_sha, manifest_sha = artifact_sha256(intent), artifact_sha256(decision), artifact_sha256(manifest)
        approval = LiveExecutionApproval.prepare(
            now_utc=now, repository_commit_sha=commit, strategy_id="btc_adaptive_dca_v1", strategy_version="1.0.0",
            canary_manifest_id="canary-1", canary_manifest_sha256=manifest_sha,
            decision_id="decision-1", decision_sha256=decision_sha, order_intent_id="intent-1", order_intent_sha256=intent_sha,
            client_order_id=client_order_id, quote_amount_usdt="25", calendar_month="2026-10",
            monthly_spent_usdt="0", remaining_monthly_budget_usdt="500", account_identity_fingerprint=digest,
            account_mode="UTA", credential_scope_classification="SPOT_TRADE_NO_WITHDRAW", liability_state="NONE",
            spot_usdt_availability_fingerprint=digest, instrument_metadata_fingerprint=digest,
            clock_skew_evidence_fingerprint=digest, reconciliation_evidence_fingerprint=digest,
            production_contract_fingerprint=digest, execution_safety_config_fingerprint=digest,
            manifest_expires_at_utc=(now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"), state="PREPARED",
        ).to_dict()
        approval_sha = artifact_sha256(approval)
        authorization = RealMoneyAuthorization.from_approval(
            LiveExecutionApproval(approval), approval_sha256=approval_sha,
            canary_manifest_id="canary-1", canary_manifest_sha256=manifest_sha, amount="25",
            client_order_id=client_order_id, calendar_month="2026-10",
            confirmation=f"AUTHORIZE BYBIT SPOT BTCUSDT BUY $25 {client_order_id}", now_utc=now,
        ).to_dict()
        authorization_sha = artifact_sha256(authorization)
        activation = {
            "activation_id": "activation-1", "created_at_utc": now.isoformat().replace("+00:00", "Z"),
            "expires_at_utc": (now + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
            "status": "ACTIVE", "consumed": False, "authorization_id": authorization["authorization_id"],
            "authorization_sha256": authorization_sha, "approval_id": approval["approval_id"], "approval_sha256": approval_sha,
            "canary_manifest_id": "canary-1", "canary_manifest_sha256": manifest_sha,
            "decision_id": "decision-1", "decision_sha256": decision_sha, "order_intent_id": "intent-1", "order_intent_sha256": intent_sha,
            "client_order_id": client_order_id, "exact_quote_amount_usdt": "25", "calendar_month": "2026-10",
            "repository_commit_sha": commit, "execution_safety_config_fingerprint": digest,
            "production_contract_fingerprint": digest, "account_identity_fingerprint": digest,
            "one_shot_identity": approval["one_shot_identity"], "no_order_executed": True,
        }
        enablement = {
            "enablement_id": "enablement-1", "created_at_utc": now.isoformat().replace("+00:00", "Z"),
            "expires_at_utc": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"), "status": "PREPARED",
            "repository_commit_sha": commit, "strategy_id": "btc_adaptive_dca_v1", "strategy_version": "1.0.0",
            "execution_policy_fingerprint": digest, "production_contract_fingerprint": digest,
            "expected_account_identity_fingerprint": digest, "expected_account_mode": "UTA", "allowed_credential_scope": "SPOT_TRADE_NO_WITHDRAW",
        }
        evidence = {"status": "EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION", "real_money_authorization": {"status": "NOT_AUTHORIZED"}, "unresolved_operations_result": {"status": "PASS"}}
        return locals()

    def test_valid_ceremony_derives_enabled_runtime_from_safe_config(self):
        c = self.ceremony()
        policy = derive_ephemeral_execution_policy(
            canonical_config=load_execution_config(), enablement=c["enablement"], enablement_sha256=artifact_sha256(c["enablement"]),
            authorization=c["authorization"], authorization_sha256=c["authorization_sha"], activation=c["activation"], activation_sha256=artifact_sha256(c["activation"]),
            approval=c["approval"], approval_sha256=c["approval_sha"], manifest=c["manifest"], manifest_sha256=c["manifest_sha"],
            decision=c["decision"], decision_sha256=c["decision_sha"], intent=c["intent"], intent_sha256=c["intent_sha"], evidence=c["evidence"],
            repository_commit_sha=c["commit"], calendar_month="2026-10", now_utc=c["now"],
        )
        self.assertTrue(policy.runtime_config.live_execution_enabled)
        self.assertFalse(load_execution_config().live_execution_enabled)
        self.assertEqual(policy.runtime_config.order_submission, "implemented")

    def test_enabled_checked_in_config_is_rejected_as_canonical_input(self):
        c = self.ceremony()
        enabled = ExecutionConfig("1.0.0", True, False, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "implemented")
        with self.assertRaises(ValueError):
            derive_ephemeral_execution_policy(
                canonical_config=enabled, enablement=c["enablement"], enablement_sha256=artifact_sha256(c["enablement"]), authorization=c["authorization"], authorization_sha256=c["authorization_sha"], activation=c["activation"], activation_sha256=artifact_sha256(c["activation"]), approval=c["approval"], approval_sha256=c["approval_sha"], manifest=c["manifest"], manifest_sha256=c["manifest_sha"], decision=c["decision"], decision_sha256=c["decision_sha"], intent=c["intent"], intent_sha256=c["intent_sha"], evidence=c["evidence"], repository_commit_sha=c["commit"], calendar_month="2026-10", now_utc=c["now"])

    def test_expired_activation_and_identity_drift_are_blocked(self):
        c = self.ceremony()
        c["activation"]["expires_at_utc"] = (c["now"] - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        with self.assertRaises(ValueError):
            derive_ephemeral_execution_policy(canonical_config=load_execution_config(), enablement=c["enablement"], enablement_sha256=artifact_sha256(c["enablement"]), authorization=c["authorization"], authorization_sha256=c["authorization_sha"], activation=c["activation"], activation_sha256=artifact_sha256(c["activation"]), approval=c["approval"], approval_sha256=c["approval_sha"], manifest=c["manifest"], manifest_sha256=c["manifest_sha"], decision=c["decision"], decision_sha256=c["decision_sha"], intent=c["intent"], intent_sha256=c["intent_sha"], evidence=c["evidence"], repository_commit_sha=c["commit"], calendar_month="2026-10", now_utc=c["now"])

    def test_runtime_blocks_mismatched_or_missing_intent_month(self):
        for month in ("2026-09", None):
            c = self.ceremony()
            if month is None:
                c["intent"].pop("calendar_month")
            else:
                c["intent"]["calendar_month"] = month
            c["intent_sha"] = artifact_sha256(c["intent"])
            with self.assertRaisesRegex(ValueError, "calendar_month"):
                derive_ephemeral_execution_policy(
                    canonical_config=load_execution_config(), enablement=c["enablement"], enablement_sha256=artifact_sha256(c["enablement"]),
                    authorization=c["authorization"], authorization_sha256=c["authorization_sha"], activation=c["activation"], activation_sha256=artifact_sha256(c["activation"]),
                    approval=c["approval"], approval_sha256=c["approval_sha"], manifest=c["manifest"], manifest_sha256=c["manifest_sha"], decision=c["decision"], decision_sha256=c["decision_sha"],
                    intent=c["intent"], intent_sha256=c["intent_sha"], evidence=c["evidence"], repository_commit_sha=c["commit"], calendar_month="2026-10", now_utc=c["now"],
                )

    def test_runtime_status_is_read_only_and_reports_absent_capability(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            before = sorted(root.rglob("*"))
            from io import StringIO
            from contextlib import redirect_stdout
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["runtime-execution-status", "--data-root", str(root)]), 0)
            result = __import__("json").loads(output.getvalue())
            self.assertEqual(result["canonical_policy"], "BLOCKED")
            self.assertEqual(result["ephemeral_capability"], "ABSENT")
            self.assertEqual(result["execution"], "BLOCKED")
            self.assertEqual(before, sorted(root.rglob("*")))


if __name__ == "__main__":
    unittest.main()
