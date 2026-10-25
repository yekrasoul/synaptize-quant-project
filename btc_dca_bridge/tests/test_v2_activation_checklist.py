from __future__ import annotations

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class V2ActivationChecklistTests(unittest.TestCase):
    def test_unverified_cutover_requirements_keep_committed_runtime_policy_disabled(self):
        checklist = yaml.safe_load((ROOT / "config" / "v2_activation_checklist.yaml").read_text(encoding="utf-8"))
        policy = yaml.safe_load((ROOT / "config" / "runtime_execution_policy.yaml").read_text(encoding="utf-8"))
        self.assertEqual(checklist["activation_decision"], "BLOCKED")
        self.assertIn("NOT_VERIFIED", set(checklist["requirements"].values()))
        self.assertEqual(checklist["requirements"]["one_reachable_production_submit_path"], "VERIFIED")
        self.assertFalse(policy["execution_enabled"])
        self.assertTrue(policy["kill_switch_active"])


if __name__ == "__main__":
    unittest.main()
