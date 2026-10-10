import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from btc_dca_bridge.cli import main
from btc_dca_bridge.config import ExecutionConfig, load_execution_config
from btc_dca_bridge.live_readiness import GateResult, GateStatus, LiveExecutionApproval
from btc_dca_bridge.operations import EXIT_BLOCKED
from btc_dca_bridge.production_enablement import ControlledProductionExecutor, GateDResult
from btc_dca_bridge.real_money_authorization import RealMoneyAuthorization, artifact_sha256
from btc_dca_bridge.runtime_execution import EphemeralExecutionPolicy


class _Transport:
    def __init__(self):
        self.calls = 0

    def submit_spot_market_buy(self, request):
        self.calls += 1
        return object()


class _Engine:
    def __init__(self, transport):
        self.transport = transport
        self.calls = 0

    def submit(self, *args, **kwargs):
        self.calls += 1
        kwargs["transport_factory"]().submit_spot_market_buy(object())
        return {"status": "mocked", "outcome_category": "acknowledgement_received"}


class CanaryExecuteWiringTests(unittest.TestCase):
    def _ceremony(self):
        now = datetime.now(UTC).replace(microsecond=0)
        sha = "a" * 64
        approval = LiveExecutionApproval.prepare(
            now_utc=now, repository_commit_sha="b" * 40,
            strategy_id="btc_adaptive_dca_v1", strategy_version="1.0.0",
            canary_manifest_id="canary-1", canary_manifest_sha256=sha,
            decision_id="decision-1", decision_sha256=sha,
            order_intent_id="intent-1", order_intent_sha256=sha,
            client_order_id="dca-" + "1" * 32, quote_amount_usdt="25",
            calendar_month="2026-10", monthly_spent_usdt="0", remaining_monthly_budget_usdt="500",
            account_identity_fingerprint=sha, account_mode="spot",
            credential_scope_classification="SPOT_TRADE_NO_WITHDRAW", liability_state="NONE",
            spot_usdt_availability_fingerprint=sha, instrument_metadata_fingerprint=sha,
            clock_skew_evidence_fingerprint=sha, reconciliation_evidence_fingerprint=sha,
            production_contract_fingerprint=sha, execution_safety_config_fingerprint=sha,
            manifest_expires_at_utc=(now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"), state="PREPARED",
        )
        approval_sha = artifact_sha256(approval.to_dict())
        authorization = RealMoneyAuthorization.from_approval(
            approval, approval_sha256=approval_sha,
            canary_manifest_id="canary-1", canary_manifest_sha256=sha,
            amount="25", client_order_id=approval.payload["client_order_id"],
            calendar_month="2026-10",
            confirmation=f"AUTHORIZE BYBIT SPOT BTCUSDT BUY $25 {approval.payload['client_order_id']}",
            now_utc=now,
        ).to_dict()
        return now, approval.to_dict(), approval_sha, authorization, artifact_sha256(authorization)

    @staticmethod
    def _gate_snapshot(approval, authorization, approval_sha, authorization_sha, config, **changes):
        current = {
            "safety_config": config,
            "real_money_authorization": authorization,
            "approval_sha256": approval_sha,
            "authorization_sha256": authorization_sha,
            "repository_commit_sha": approval["repository_commit_sha"],
            "calendar_month": approval["calendar_month"],
            "monthly_spent_usdt": approval["monthly_spent_usdt"],
            "remaining_monthly_budget_usdt": approval["remaining_monthly_budget_usdt"],
            "account_identity_fingerprint": approval["account_identity_fingerprint"],
            "account_mode": approval["account_mode"],
            "credential_scope_classification": approval["credential_scope_classification"],
            "liability_state": approval["liability_state"],
            "spot_usdt_availability_fingerprint": approval["spot_usdt_availability_fingerprint"],
            "instrument_metadata_fingerprint": approval["instrument_metadata_fingerprint"],
            "clock_skew_evidence_fingerprint": approval["clock_skew_evidence_fingerprint"],
            "reconciliation_evidence_fingerprint": approval["reconciliation_evidence_fingerprint"],
            "production_contract_fingerprint": approval["production_contract_fingerprint"],
            "execution_safety_config_fingerprint": approval["execution_safety_config_fingerprint"],
            "ledger_valid": True, "credential_scope_accepted": True, "withdrawal_enabled": False,
            "btc_liability": False, "usdt_liability": False, "borrow_state": False,
            "spot_usdt_sufficient": True, "spot_availability_fresh": True,
            "instrument_metadata_fresh": True, "clock_evidence_fresh": True,
            "reconciliation_evidence_fresh": True, "duplicate_execution": False,
            "duplicate_attempt": False, "reconciliation_required": False,
            "request_fingerprint": "c" * 64,
        }
        current.update(changes)
        return current

    def test_checked_in_defaults_never_construct_transport_or_post(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = StringIO()
            with patch("btc_dca_bridge.cli._submission_transport_factory") as factory, redirect_stdout(output):
                code = main([
                    "canary-execute", "--run-id", "run_20261010T120000Z_wiretest1",
                    "--canary-id", "canary-missing", "--approval-id", "approval-missing",
                    "--manifest-sha", "a" * 64, "--approval-sha", "b" * 64,
                    "--data-root", str(root / "data"), "--ledger", str(root / "ledger.jsonl"),
                    "--month", "2026-10",
                ])
            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(json.loads(output.getvalue())["outcome_category"], "request_not_sent")
            factory.assert_not_called()

    def test_controlled_executor_enabled_fixture_invokes_lazy_transport_once(self):
        now = datetime(2026, 10, 10, 12, tzinfo=UTC)
        config = ExecutionConfig("1.0.0", True, False, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "implemented")
        transport = _Transport()
        engine = _Engine(transport)
        store = object()
        ready_d = GateDResult(GateStatus.READY_FOR_SUBMISSION, (), "d" * 64)
        ready = GateResult(GateStatus.READY_FOR_SUBMISSION, "A", (), "a" * 64)
        approval = {"approval_id": "approval", "status": "PREPARED", "state": "PREPARED"}
        activation = {"activation_id": "activation", "status": "ACTIVE", "consumed": False}

        with patch("btc_dca_bridge.production_enablement.gate_d", return_value=ready_d), \
             patch.object(ControlledProductionExecutor, "_verify_persisted_chain"), \
             patch("btc_dca_bridge.production_enablement.LiveExecutionGate") as gate_cls, \
             patch("btc_dca_bridge.production_enablement.issue_execution_capability", return_value=object()):
            gate_cls.return_value.evaluate_a_then_b.return_value = (ready, ready)
            result = ControlledProductionExecutor(engine=engine, store=store).execute(
                enablement={"enablement_id": "enablement"}, enablement_sha256="e" * 64,
                authorization={"authorization_id": "authorization", "status": "AUTHORIZED", "consumed": False}, authorization_sha256="f" * 64,
                activation=activation, activation_sha256="g" * 64, capability=None,
                approval=approval, approval_sha256="h" * 64,
                manifest={"canary_id": "canary"}, manifest_sha256="i" * 64,
                intent=object(), decision={}, runtime_config=config, evidence={},
                repository_commit_sha="j" * 40, calendar_month="2026-10", ledger_path=Path("/dev/null"),
                read_client=object(), reconciler=object(), run_id="run_20261010T120000Z_wiretest2",
                transport_factory=lambda: transport,
                gate_a_current={"request_fingerprint": "k" * 64}, gate_b_current={"request_fingerprint": "k" * 64},
            )
        self.assertEqual(result["status"], "mocked")
        self.assertEqual(engine.calls, 1)
        self.assertEqual(transport.calls, 1)

    def test_gate_d_blocks_before_engine_or_transport(self):
        engine = Mock()
        transport = Mock()
        config = load_execution_config()
        result = ControlledProductionExecutor(engine=engine, store=unittest.mock.Mock()).execute(
            enablement=None, enablement_sha256="a" * 64,
            authorization={}, authorization_sha256="b" * 64,
            activation={}, activation_sha256="c" * 64, capability=None,
            approval={}, approval_sha256="d" * 64, manifest={}, manifest_sha256="e" * 64,
            intent=Mock(), decision={}, runtime_config=config, evidence={},
            repository_commit_sha="f" * 40, calendar_month="2026-10", ledger_path=Path("/dev/null"),
            read_client=None, reconciler=None, run_id="run_20261010T120000Z_wiretest3",
            transport_factory=lambda: transport,
        )
        self.assertEqual(result["gate"], "D")
        engine.submit.assert_not_called()
        transport.assert_not_called()

    def test_cli_enabled_path_collects_two_fresh_snapshots_and_posts_once(self):
        now, approval, approval_sha, authorization, authorization_sha = self._ceremony()
        canonical_config = load_execution_config()
        runtime_config = ExecutionConfig("1.0.0", True, False, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "implemented")
        snapshots = [self._gate_snapshot(approval, authorization, approval_sha, authorization_sha, runtime_config), self._gate_snapshot(approval, authorization, approval_sha, authorization_sha, runtime_config)]
        policy = EphemeralExecutionPolicy(runtime_config=runtime_config, one_shot_identity=approval["one_shot_identity"], client_order_id=approval["client_order_id"], quote_amount_usdt=approval["quote_amount_usdt"], calendar_month=approval["calendar_month"], expires_at_utc="2099-01-01T00:00:00Z", request_fingerprint="c" * 64)
        transport = _Transport()
        engine = _Engine(transport)
        intent = SimpleNamespace(order_intent_id="intent-1", decision_id="decision-1", client_order_id=approval["client_order_id"], quote_amount_usdt=Decimal("25"))
        chain = {
            "store": Mock(), "manifest": {"canary_id": "canary-1"}, "manifest_sha": "d" * 64,
            "approval": approval, "approval_sha": approval_sha,
            "authorization": authorization, "authorization_sha": authorization_sha,
            "activation": {"activation_id": "activation-1", "status": "ACTIVE", "consumed": False}, "activation_sha": "e" * 64,
            "enablement": {"enablement_id": "enablement-1"}, "enablement_sha": "f" * 64,
            # Persisted evidence deliberately contains stale/inapplicable gate
            # keys; the CLI must ignore them and use the two fresh probes.
            "evidence": {"status": "EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION", "real_money_authorization": {"status": "NOT_AUTHORIZED"}, "gate_a": {"account_identity_fingerprint": "z" * 64}, "gate_b": {"account_identity_fingerprint": "z" * 64}}, "evidence_sha": "1" * 64,
            "decision": {"decision_id": "decision-1"}, "decision_sha": "2" * 64, "intent": intent, "intent_sha": "3" * 64,
        }
        ready_d = GateDResult(GateStatus.READY_FOR_SUBMISSION, (), "4" * 64)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = StringIO()
            with patch("btc_dca_bridge.cli.load_execution_config", return_value=canonical_config), \
                 patch("btc_dca_bridge.cli._load_controlled_execution_artifacts", return_value=chain), \
                 patch("btc_dca_bridge.cli.derive_ephemeral_execution_policy", return_value=policy), \
                 patch("btc_dca_bridge.cli.gate_d", return_value=ready_d), \
                 patch("btc_dca_bridge.production_enablement.gate_d", return_value=ready_d), \
                 patch("btc_dca_bridge.cli._private_read_client_factory", return_value=Mock()), \
                 patch("btc_dca_bridge.cli._post_ack_reconciler_factory", return_value=Mock()), \
                 patch("btc_dca_bridge.cli._submission_transport_factory", return_value=transport) as transport_factory, \
                 patch("btc_dca_bridge.cli._fresh_gate_snapshot", side_effect=snapshots) as fresh, \
                 redirect_stdout(output):
                # Use the real executor instance so the CLI's transport factory
                # is exercised, while bypassing filesystem-only chain setup.
                with patch("btc_dca_bridge.cli.ControlledProductionExecutor", side_effect=lambda **kwargs: ControlledProductionExecutor(engine=engine, store=chain["store"])), \
                     patch("btc_dca_bridge.production_enablement.ControlledProductionExecutor._verify_persisted_chain"), \
                     patch("btc_dca_bridge.production_enablement.issue_execution_capability", return_value=object()):
                    code = main([
                        "canary-execute", "--run-id", "run_20261010T120000Z_wiretest4",
                        "--canary-id", "canary-1", "--approval-id", approval["approval_id"],
                        "--manifest-sha", "d" * 64, "--approval-sha", approval_sha,
                        "--data-root", str(root / "data"), "--ledger", str(root / "ledger.jsonl"), "--month", "2026-10",
                    ])
            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(fresh.call_count, 2)
            self.assertEqual(fresh.call_args_list[0].kwargs["now_utc"].tzinfo, UTC)
            self.assertEqual(fresh.call_args_list[1].kwargs["now_utc"].tzinfo, UTC)
            self.assertEqual(engine.calls, 1)
            self.assertEqual(transport.calls, 1)
            transport_factory.assert_called_once()

    def test_cli_gate_b_drift_prevents_transport(self):
        now, approval, approval_sha, authorization, authorization_sha = self._ceremony()
        canonical_config = load_execution_config()
        runtime_config = ExecutionConfig("1.0.0", True, False, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "implemented")
        first = self._gate_snapshot(approval, authorization, approval_sha, authorization_sha, runtime_config)
        second = self._gate_snapshot(approval, authorization, approval_sha, authorization_sha, runtime_config, account_identity_fingerprint="c" * 64)
        policy = EphemeralExecutionPolicy(runtime_config=runtime_config, one_shot_identity=approval["one_shot_identity"], client_order_id=approval["client_order_id"], quote_amount_usdt=approval["quote_amount_usdt"], calendar_month=approval["calendar_month"], expires_at_utc="2099-01-01T00:00:00Z", request_fingerprint="c" * 64)
        transport = _Transport()
        engine = _Engine(transport)
        ready_d = GateDResult(GateStatus.READY_FOR_SUBMISSION, (), "4" * 64)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chain = {"store": Mock(), "manifest": {}, "manifest_sha": "d" * 64, "approval": approval, "approval_sha": approval_sha, "authorization": authorization, "authorization_sha": authorization_sha, "activation": {"activation_id": "a", "status": "ACTIVE", "consumed": False}, "activation_sha": "e" * 64, "enablement": {}, "enablement_sha": "f" * 64, "evidence": {"status": "EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION", "real_money_authorization": {"status": "NOT_AUTHORIZED"}}, "evidence_sha": "1" * 64, "decision": {}, "decision_sha": "2" * 64, "intent": SimpleNamespace(order_intent_id="intent-1", decision_id="decision-1", client_order_id=approval["client_order_id"], quote_amount_usdt=Decimal("25")), "intent_sha": "3" * 64}
            with patch("btc_dca_bridge.cli.load_execution_config", return_value=canonical_config), patch("btc_dca_bridge.cli._load_controlled_execution_artifacts", return_value=chain), patch("btc_dca_bridge.cli.derive_ephemeral_execution_policy", return_value=policy), patch("btc_dca_bridge.cli.gate_d", return_value=ready_d), patch("btc_dca_bridge.cli._private_read_client_factory", return_value=Mock()), patch("btc_dca_bridge.cli._post_ack_reconciler_factory", return_value=Mock()), patch("btc_dca_bridge.cli._fresh_gate_snapshot", side_effect=[first, second]), patch("btc_dca_bridge.cli.ControlledProductionExecutor", side_effect=lambda **kwargs: ControlledProductionExecutor(engine=engine, store=chain["store"])), patch("btc_dca_bridge.production_enablement.gate_d", return_value=ready_d), patch("btc_dca_bridge.production_enablement.ControlledProductionExecutor._verify_persisted_chain"), patch("btc_dca_bridge.production_enablement.issue_execution_capability", return_value=object()):
                code = main(["canary-execute", "--run-id", "run_20261010T120000Z_wiretest5", "--canary-id", "canary-1", "--approval-id", approval["approval_id"], "--manifest-sha", "d" * 64, "--approval-sha", approval_sha, "--data-root", str(root / "data"), "--ledger", str(root / "ledger.jsonl"), "--month", "2026-10"])
            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(engine.calls, 0)
            self.assertEqual(transport.calls, 0)


if __name__ == "__main__":
    unittest.main()
