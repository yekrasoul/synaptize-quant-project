import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType
from btc_dca_bridge.config import ExecutionConfig, load_execution_config
from btc_dca_bridge.live_readiness import (
    GateStatus,
    LiveExecutionApproval,
    LiveExecutionGate,
    LiveReadinessError,
    canonical_fingerprint,
    classify_submission_outcome,
    ledger_append_allowed,
    prepare_submission_attempt,
    submit_disabled,
)
from btc_dca_bridge.real_money_authorization import RealMoneyAuthorization, artifact_sha256
from btc_dca_bridge.schemas import validate_artifact


NOW = datetime(2026, 10, 10, 17, 32, tzinfo=UTC)
SHA = "a" * 64


def safe_config(**changes):
    values = dict(schema_version="1.0.0", live_execution_enabled=True, kill_switch=False,
                  explicit_live_approval_required=True, monthly_cap_usd=500,
                  exchange="Bybit", market_type="spot", symbol="BTCUSDT",
                  order_submission="implemented_disabled")
    values.update(changes)
    return ExecutionConfig(**values)


class LiveReadinessTests(unittest.TestCase):
    def approval(self, **changes):
        values = dict(
            now_utc=NOW, repository_commit_sha="b" * 40,
            strategy_id="btc_adaptive_dca_v1", strategy_version="1.0.0",
            canary_manifest_id="canary-1", canary_manifest_sha256=SHA,
            decision_id="decision-1", decision_sha256=SHA,
            order_intent_id="intent-1", order_intent_sha256=SHA,
            client_order_id="dca-" + "1" * 32, quote_amount_usdt="25",
            calendar_month="2026-10", monthly_spent_usdt="0",
            remaining_monthly_budget_usdt="500", account_identity_fingerprint=SHA,
            account_mode="spot", credential_scope_classification="SPOT_TRADE_NO_WITHDRAW",
            liability_state="NONE", spot_usdt_availability_fingerprint=SHA,
            instrument_metadata_fingerprint=SHA, clock_skew_evidence_fingerprint=SHA,
            reconciliation_evidence_fingerprint=SHA, production_contract_fingerprint=SHA,
            execution_safety_config_fingerprint=SHA,
            manifest_expires_at_utc="2026-10-10T17:42:00Z",
        )
        values.update(changes)
        return LiveExecutionApproval.prepare(**values)

    def current(self, approval, **changes):
        p = approval.payload
        approval_sha = artifact_sha256(p)
        authorization = RealMoneyAuthorization.from_approval(
            approval, approval_sha256=approval_sha,
            canary_manifest_id=p["canary_manifest_id"], canary_manifest_sha256=p["canary_manifest_sha256"],
            amount=p["quote_amount_usdt"], client_order_id=p["client_order_id"],
            calendar_month=p["calendar_month"],
            confirmation=f"AUTHORIZE BYBIT SPOT BTCUSDT BUY ${p['quote_amount_usdt']} {p['client_order_id']}",
            now_utc=NOW,
        )
        authorization_sha = artifact_sha256(authorization.to_dict())
        values = dict(
            safety_config=safe_config(), real_money_authorization=authorization.to_dict(),
            approval_sha256=approval_sha, authorization_sha256=authorization_sha,
            repository_commit_sha=p["repository_commit_sha"], calendar_month=p["calendar_month"],
            monthly_spent_usdt=p["monthly_spent_usdt"], remaining_monthly_budget_usdt=p["remaining_monthly_budget_usdt"],
            account_identity_fingerprint=p["account_identity_fingerprint"], account_mode=p["account_mode"],
            credential_scope_classification=p["credential_scope_classification"], liability_state=p["liability_state"],
            spot_usdt_availability_fingerprint=p["spot_usdt_availability_fingerprint"],
            instrument_metadata_fingerprint=p["instrument_metadata_fingerprint"],
            clock_skew_evidence_fingerprint=p["clock_skew_evidence_fingerprint"],
            reconciliation_evidence_fingerprint=p["reconciliation_evidence_fingerprint"],
            production_contract_fingerprint=p["production_contract_fingerprint"],
            execution_safety_config_fingerprint=p["execution_safety_config_fingerprint"],
            ledger_valid=True, credential_scope_accepted=True, withdrawal_enabled=False,
            btc_liability=False, usdt_liability=False, borrow_state=False,
            spot_usdt_sufficient=True, spot_availability_fresh=True,
            instrument_metadata_fresh=True, clock_evidence_fresh=True,
            reconciliation_evidence_fresh=True, duplicate_execution=False,
            duplicate_attempt=False, reconciliation_required=False,
        )
        values.update(changes)
        return values

    def test_approval_is_schema_valid_immutable_and_not_authorization(self):
        approval = self.approval()
        self.assertEqual(approval.payload["state"], "BLOCKED")
        self.assertEqual(approval.payload["explicit_authorization_status"], "NOT_AUTHORIZED")
        self.assertEqual(approval.payload["order_contract"]["marketUnit"], "quoteCoin")
        validate_artifact("live_execution_approval", approval.to_dict())
        self.assertEqual(approval.approval_id, self.approval().approval_id)
        self.assertNotIn("secret", str(approval.to_dict()).lower())

    def test_approval_ttl_is_at_most_ten_minutes_and_expired_blocks(self):
        with self.assertRaises(LiveReadinessError): self.approval(ttl=timedelta(minutes=11))
        approval = self.approval(state="PREPARED")
        result = LiveExecutionGate(approval, now=lambda: NOW + timedelta(minutes=11)).evaluate(self.current(approval))
        self.assertEqual(result.status, GateStatus.BLOCKED)
        self.assertIn("expired", " ".join(result.reasons))

    def test_current_checked_in_safety_blocks_before_submission(self):
        approval = self.approval(state="PREPARED")
        blocked = LiveExecutionGate(approval, now=lambda: NOW).evaluate(self.current(approval, safety_config=load_execution_config(), authorization_status="AUTHORIZED"))
        self.assertEqual(blocked.status, GateStatus.BLOCKED)
        self.assertIn("kill switch", " ".join(blocked.reasons))
        self.assertIn("live execution", " ".join(blocked.reasons))

    def test_each_safety_and_evidence_gate_fails_closed(self):
        approval = self.approval(state="PREPARED")
        base = self.current(approval)
        cases = {
            "kill": {"safety_config": safe_config(kill_switch=True)},
            "disabled": {"safety_config": safe_config(live_execution_enabled=False)},
            "no-auth": {"real_money_authorization": None},
            "manifest": {"canary_manifest_sha256": "c" * 64},
            "month": {"calendar_month": "2026-11"},
            "spent": {"monthly_spent_usdt": "490", "remaining_monthly_budget_usdt": "10"},
            "duplicate": {"duplicate_attempt": True},
            "wrong-account": {"account_identity_fingerprint": "c" * 64},
            "scope": {"credential_scope_accepted": False},
            "withdraw": {"withdrawal_enabled": True},
            "btc-liability": {"btc_liability": True},
            "usdt-liability": {"usdt_liability": True},
            "borrow": {"borrow_state": True},
            "balance": {"spot_usdt_sufficient": False},
            "stale-balance": {"spot_availability_fresh": False},
            "stale-instrument": {"instrument_metadata_fresh": False},
            "clock": {"clock_evidence_fresh": False},
            "reconciliation": {"reconciliation_required": True},
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                result = LiveExecutionGate(approval, now=lambda: NOW).evaluate({**base, **change})
                self.assertEqual(result.status, GateStatus.BLOCKED)

    def test_gate_a_then_b_blocks_changed_current_evidence(self):
        approval = self.approval(state="PREPARED")
        first, second = LiveExecutionGate(approval, now=lambda: NOW).evaluate_a_then_b(self.current(approval), self.current(approval, account_identity_fingerprint="c" * 64))
        self.assertEqual(first.status, GateStatus.READY_FOR_SUBMISSION)
        self.assertEqual(second.status, GateStatus.BLOCKED)
        self.assertIn("account identity changed", " ".join(second.reasons))

    def test_attempt_is_append_only_before_future_network_and_disabled_transport(self):
        root = Path(tempfile.mkdtemp())
        store = ArtifactStore(root)
        approval = self.approval(state="PREPARED")
        gate = LiveExecutionGate(approval, now=lambda: NOW).evaluate(self.current(approval))
        approval_receipt = approval.persist(store, run_id="run_20261010T173200Z_approval01")
        receipt = prepare_submission_attempt(store=store, run_id="run_20261010T173200Z_attempt01", approval=approval, approval_sha256=approval_receipt.sha256, gate_result=gate, repository_commit_sha="b" * 40, decision_id="decision-1", decision_sha256=SHA, order_intent_id="intent-1", order_intent_sha256=SHA, quote_amount_usdt="25", account_identity_fingerprint=SHA, calendar_month="2026-10", reconciliation_fingerprint=SHA)
        self.assertEqual(receipt.artifact_type, ArtifactType.LIVE_EXECUTION_ATTEMPT)
        self.assertTrue(receipt.path.exists())
        validate_artifact("live_execution_attempt", store.read(ArtifactType.LIVE_EXECUTION_ATTEMPT, run_id=receipt.run_id, artifact_date_utc=NOW))
        with self.assertRaisesRegex(LiveReadinessError, "NOT AUTHORIZED"):
            submit_disabled()

    def test_blocked_gate_cannot_create_attempt_and_no_ledger_mutation(self):
        root = Path(tempfile.mkdtemp()); store = ArtifactStore(root)
        approval = self.approval(state="PREPARED")
        gate = LiveExecutionGate(approval, now=lambda: NOW).evaluate(self.current(approval, authorization_status="NOT_AUTHORIZED"))
        with self.assertRaises(LiveReadinessError):
            prepare_submission_attempt(store=store, run_id="run_20261010T173200Z_attempt02", approval=approval, approval_sha256=SHA, gate_result=gate, repository_commit_sha="b" * 40, decision_id="decision-1", decision_sha256=SHA, order_intent_id="intent-1", order_intent_sha256=SHA, quote_amount_usdt="25", account_identity_fingerprint=SHA, calendar_month="2026-10", reconciliation_fingerprint=SHA)
        self.assertFalse((root / "live_execution_attempts").exists())

    def test_ambiguous_submission_never_allows_retry_or_ledger_append(self):
        ambiguous = {"client_order_id": "dca-" + "1" * 32, "reconciliation_state": "partial", "fills": []}
        self.assertEqual(classify_submission_outcome(ambiguous), "RECONCILIATION_REQUIRED")
        self.assertFalse(ledger_append_allowed(ambiguous))
        self.assertEqual(classify_submission_outcome({"client_order_id": "dca-" + "1" * 32, "reconciliation_state": "confirmed", "fills": [{"exec_id": "e"}]}), "CONFIRMED_EXECUTION")
        self.assertTrue(ledger_append_allowed({"client_order_id": "dca-" + "1" * 32, "reconciliation_state": "confirmed", "fills": [{"exec_id": "e"}], "ledger_mutated": False}))


if __name__ == "__main__": unittest.main()
