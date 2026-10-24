import tempfile
import unittest
from pathlib import Path

from btc_dca_bridge.final_readiness import final_readiness_audit


class FinalReadinessTests(unittest.TestCase):
    def test_current_installation_is_blocked_and_does_not_mutate_local_data(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = final_readiness_audit(
                ledger_path=Path(__file__).parents[1] / "ledger" / "executions.jsonl",
                data_root=root,
            )
            self.assertEqual(result["readiness"], "BLOCKED")
            self.assertFalse(result["real_money_execution_possible"])
            self.assertEqual(result["production_enablement"], "NOT_ENABLED")
            self.assertEqual(result["authorization"], "NOT_AUTHORIZED")
            self.assertEqual(result["activation"], "NOT_ACTIVATED")
            self.assertEqual(result["execution"], "BLOCKED")
            self.assertEqual(list(root.rglob("*")), [])


if __name__ == "__main__":
    unittest.main()
