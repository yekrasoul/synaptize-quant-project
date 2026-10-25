from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from btc_dca_bridge.cli import main
from btc_dca_bridge.config import load_execution_config
from btc_dca_bridge.infrastructure.policy import load_runtime_execution_policy


class WriterReachabilityBaselineTests(unittest.TestCase):
    """Canonical pre-cutover invariant: checked-in main exposes zero writers."""

    def test_checked_in_policy_blocks_both_legacy_and_v2_execution(self):
        legacy = load_execution_config()
        v2 = load_runtime_execution_policy(Path("config/runtime_execution_policy.yaml"))

        self.assertFalse(legacy.live_execution_enabled)
        self.assertTrue(legacy.kill_switch)
        self.assertFalse(v2.execution_enabled)
        self.assertTrue(v2.kill_switch_active)

    def test_production_entrypoints_reach_zero_order_create_writers(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            decision = root / "decision.json"
            decision.write_text(
                json.dumps(
                    {
                        "decision_id": "decision-writer-reachability",
                        "strategy_id": "btc_adaptive_dca_v1",
                        "strategy_version": "1.0.0",
                        "final_purchase_usd": 25,
                    }
                ),
                encoding="utf-8",
            )
            data_root = root / "data"
            ledger = root / "ledger.jsonl"
            ledger.write_text("", encoding="utf-8")

            output = StringIO()
            with (
                patch(
                    "btc_dca_bridge.venues.ccxt.adapter.CcxtVenueAdapter.bybit"
                ) as ccxt_writer_constructor,
                patch(
                    "btc_dca_bridge.execution_v2.service.ExecutionService.execute"
                ) as v2_execute,
                patch(
                    "btc_dca_bridge.cli._submission_transport_factory"
                ) as legacy_transport_factory,
                patch(
                    "btc_dca_bridge.cli._private_read_client_factory"
                ) as private_read_factory,
                redirect_stdout(output),
            ):
                execute_code = main(
                    ["execute", "--decision-json", str(decision)]
                )
                live_submit_code = main(
                    ["live-submit", "--decision-json", str(decision)]
                )
                canary_code = main(
                    [
                        "canary-execute",
                        "--run-id",
                        "run_20261025T000000Z_writercheck",
                        "--canary-id",
                        "canary-writer-check",
                        "--approval-id",
                        "approval-writer-check",
                        "--manifest-sha",
                        "a" * 64,
                        "--approval-sha",
                        "b" * 64,
                        "--data-root",
                        str(data_root),
                        "--ledger",
                        str(ledger),
                        "--month",
                        "2026-10",
                    ]
                )

            self.assertEqual(execute_code, 2)
            self.assertEqual(live_submit_code, 0)
            self.assertEqual(canary_code, 2)

            ccxt_writer_constructor.assert_not_called()
            v2_execute.assert_not_called()
            legacy_transport_factory.assert_not_called()
            private_read_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
