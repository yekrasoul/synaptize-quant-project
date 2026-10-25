from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from btc_dca_bridge.cli import main
from btc_dca_bridge.config import load_execution_config
from btc_dca_bridge.infrastructure.policy import V2ActivationGate, load_runtime_execution_policy


class WriterReachabilityCutoverTests(unittest.TestCase):
    def _decision(self, root: Path) -> Path:
        path = root / "decision.json"
        path.write_text(
            json.dumps(
                {
                    "decision_id": "decision_single_writer",
                    "created_at_utc": "2026-10-25T22:00:00Z",
                    "strategy_id": "btc_adaptive_dca_v1",
                    "strategy_version": "1.0.0",
                    "final_purchase_usd": 25,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_checked_in_main_remains_fail_closed_before_runtime_or_credentials(self):
        with TemporaryDirectory() as raw:
            decision = self._decision(Path(raw))
            legacy = load_execution_config()
            v2 = load_runtime_execution_policy(Path("config/runtime_execution_policy.yaml"))
            self.assertFalse(legacy.live_execution_enabled)
            self.assertTrue(legacy.kill_switch)
            self.assertFalse(v2.execution_enabled)
            self.assertTrue(v2.kill_switch_active)

            with (
                patch("btc_dca_bridge.cli._v2_execution_runtime_factory") as runtime_factory,
                patch("btc_dca_bridge.cli._private_read_client_factory") as private_factory,
                redirect_stdout(StringIO()),
            ):
                code = main(["execute", "--decision-json", str(decision)])
            self.assertEqual(code, 2)
            runtime_factory.assert_not_called()
            private_factory.assert_not_called()

    def test_legacy_canary_writer_is_permanently_retired_before_dependencies(self):
        with (
            patch("btc_dca_bridge.cli._submission_transport_factory") as transport_factory,
            patch("btc_dca_bridge.cli._private_read_client_factory") as private_factory,
            patch("btc_dca_bridge.cli._load_controlled_execution_artifacts") as artifact_loader,
            redirect_stdout(StringIO()),
        ):
            code = main(
                [
                    "canary-execute",
                    "--run-id",
                    "run_20261025T220000Z_retired",
                    "--canary-id",
                    "canary-retired",
                    "--approval-id",
                    "approval-retired",
                    "--manifest-sha",
                    "a" * 64,
                    "--approval-sha",
                    "b" * 64,
                    "--month",
                    "2026-10",
                ]
            )
        self.assertEqual(code, 2)
        transport_factory.assert_not_called()
        private_factory.assert_not_called()
        artifact_loader.assert_not_called()

    def test_gate_ready_execute_routes_only_to_v2_runtime(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            decision = self._decision(root)
            policy = root / "policy.yaml"
            policy.write_text(
                "\n".join(
                    (
                        "execution_enabled: true",
                        "kill_switch_active: false",
                        "monthly_cap_usd: 500",
                        "allowed_venue: bybit",
                        "allowed_market: BTC/USDT:spot",
                        "expected_account_fingerprint: account-1",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            fake_intent = SimpleNamespace(
                intent_id="intent-v2-only",
                client_order_id="v2-only-client",
            )
            fake_order = SimpleNamespace(
                state=SimpleNamespace(value="CONFIRMED"),
                venue_order_id="venue-1",
            )
            runtime = Mock()
            runtime.intent_from_decision.return_value = fake_intent
            runtime.context_provider.current.return_value = object()
            runtime.execute.return_value = SimpleNamespace(
                order=fake_order,
                risk_status="APPROVED",
                reason_codes=(),
            )

            with (
                patch(
                    "btc_dca_bridge.cli.load_v2_activation_gate",
                    return_value=V2ActivationGate(True, ()),
                ),
                patch(
                    "btc_dca_bridge.cli._v2_execution_runtime_factory",
                    return_value=runtime,
                ) as v2_factory,
                patch("btc_dca_bridge.cli._submission_transport_factory") as legacy_factory,
                redirect_stdout(StringIO()),
            ):
                code = main(
                    [
                        "execute",
                        "--decision-json",
                        str(decision),
                        "--policy",
                        str(policy),
                        "--data-root",
                        str(root / "data"),
                        "--ledger",
                        str(root / "ledger.jsonl"),
                    ]
                )
            self.assertEqual(code, 0)
            v2_factory.assert_called_once()
            runtime.execute.assert_called_once()
            legacy_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
