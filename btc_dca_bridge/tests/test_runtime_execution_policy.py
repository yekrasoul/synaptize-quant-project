from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from btc_dca_bridge.infrastructure.policy import load_runtime_execution_policy


class RuntimeExecutionPolicyTests(unittest.TestCase):
    def test_default_policy_is_fail_closed_and_preserves_cap(self):
        policy = load_runtime_execution_policy(Path(__file__).parents[1] / "config" / "runtime_execution_policy.yaml")
        self.assertEqual((policy.execution_enabled, policy.kill_switch_active, policy.monthly_cap_usd), (False, True, Decimal("500")))

    def test_policy_rejects_drifted_cap_or_extra_switch(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "policy.yaml"
            path.write_text("execution_enabled: true\nkill_switch_active: false\nmonthly_cap_usd: 501\nallowed_venue: bybit\nallowed_market: BTC/USDT:spot\nexpected_account_fingerprint: x\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_runtime_execution_policy(path)


if __name__ == "__main__":
    unittest.main()
