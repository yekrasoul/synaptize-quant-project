import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from btc_dca_bridge.cli import main
from btc_dca_bridge.config import ExecutionConfig, load_execution_config
from btc_dca_bridge.live_readiness import GateResult, GateStatus
from btc_dca_bridge.operations import EXIT_BLOCKED
from btc_dca_bridge.production_enablement import ControlledProductionExecutor, GateDResult


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


if __name__ == "__main__":
    unittest.main()
