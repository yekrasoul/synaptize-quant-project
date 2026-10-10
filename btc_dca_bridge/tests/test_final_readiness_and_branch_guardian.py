import tempfile
import unittest
from pathlib import Path
import yaml

from btc_dca_bridge.branch_guardian import changed_diff_lines, classify_branch
from btc_dca_bridge.final_readiness import final_readiness_audit


SAFE = {"execution_config_safe": True, "authorization": "NOT_AUTHORIZED", "activation": "NOT_ACTIVATED", "production_enablement": "NOT_ENABLED", "execution": "BLOCKED"}


class BranchGuardianTests(unittest.TestCase):
    def classify(self, files=(), diff="code change", runtime=None, bootstrap=False):
        return classify_branch(branch="feat/example", head_sha="h" * 40, base_sha="b" * 40, changed_files=files, diff_text=diff, runtime=runtime or SAFE, bootstrap=bootstrap)

    def test_normal_docs_and_tests_are_safe_engineering(self):
        self.assertEqual(self.classify(["README.md"])["classification"], "SAFE_ENGINEERING")
        self.assertEqual(self.classify(["tests/test_new.py"])["classification"], "SAFE_ENGINEERING")
        self.assertEqual(self.classify(["docs/note.md"])["classification"], "SAFE_ENGINEERING")

    def test_protected_surfaces_are_safety_sensitive(self):
        for path in ("btc_dca_bridge/config/execution.yaml", "btc_dca_bridge/config/active_strategy.json", ".github/workflows/production-shadow.yml"):
            self.assertEqual(self.classify([path])["classification"], "SAFETY_SENSITIVE")

    def test_safety_semantic_changes_are_conservative_blockers(self):
        for diff in ("+kill_switch: false", "+live_execution_enabled: true", "+order_submission: implemented", "+monthly_cap_usd: 501", "+market: Other", "+leverage=1", "+bypass gate", "+POST /v5/order/create"):
            self.assertEqual(self.classify(diff=diff)["classification"], "SAFETY_SENSITIVE")

    def test_changed_line_extraction_ignores_context_and_metadata(self):
        diff = "\n".join((
            "diff --git a/docs/BRANCH_GUARDIAN.md b/docs/BRANCH_GUARDIAN.md",
            "index 1111111..2222222 100644", "--- a/docs/BRANCH_GUARDIAN.md", "+++ b/docs/BRANCH_GUARDIAN.md",
            "@@ -1,3 +1,3 @@", " kill_switch live_execution_enabled order_submission Bybit BTCUSDT",
            "+Pilot verified after bootstrap.",
        ))
        self.assertEqual(changed_diff_lines(diff), "Pilot verified after bootstrap.")
        self.assertEqual(self.classify(files=["docs/BRANCH_GUARDIAN.md"], diff=diff)["classification"], "SAFE_ENGINEERING")

    def test_added_and_removed_safety_lines_are_blocked(self):
        for diff in ("+kill_switch: false", "-kill_switch: true"):
            self.assertEqual(self.classify(diff=diff)["classification"], "SAFETY_SENSITIVE")

    def test_diff_headers_cannot_trigger_safety_classification(self):
        diff = "--- a/docs/kill_switch.md\n+++ b/docs/live_execution_enabled.md\n@@ -1 +1 @@\n context"
        self.assertEqual(changed_diff_lines(diff), "")
        self.assertEqual(self.classify(files=["docs/note.md"], diff=diff)["classification"], "SAFE_ENGINEERING")

    def test_ledger_and_runtime_drift_fail(self):
        self.assertEqual(self.classify(["btc_dca_bridge/ledger/executions.jsonl"])["classification"], "FAIL")
        runtime = dict(SAFE, authorization="AUTHORIZED")
        self.assertEqual(self.classify(runtime=runtime)["classification"], "FAIL")
        self.assertFalse(self.classify(bootstrap=True)["auto_merge_eligible"])


class FinalReadinessTests(unittest.TestCase):
    def test_current_installation_is_blocked_and_does_not_mutate_local_data(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = final_readiness_audit(ledger_path=Path(__file__).parents[1] / "ledger" / "executions.jsonl", data_root=root)
            self.assertEqual(result["readiness"], "BLOCKED")
            self.assertFalse(result["real_money_execution_possible"])
            self.assertEqual(result["production_enablement"], "NOT_ENABLED")
            self.assertEqual(result["authorization"], "NOT_AUTHORIZED")
            self.assertEqual(result["activation"], "NOT_ACTIVATED")
            self.assertEqual(result["execution"], "BLOCKED")
            self.assertEqual(list(root.rglob("*")), [])

    def test_guardian_workflow_is_scoped_and_bootstrapped(self):
        workflow = yaml.safe_load((Path(__file__).parents[2] / ".github" / "workflows" / "branch-guardian.yml").read_text(encoding="utf-8"))
        self.assertEqual(workflow["permissions"], {})
        triggers = workflow.get("on", workflow.get(True))
        branches = triggers["push"]["branches"]
        self.assertEqual(set(branches), {"feat/**", "fix/**", "refactor/**"})
        self.assertEqual(workflow["jobs"]["validation"]["permissions"], {"contents": "read", "actions": "read"})
        self.assertEqual(workflow["jobs"]["guardian"]["permissions"]["pull-requests"], "write")
        workflow_text = (Path(__file__).parents[2] / ".github" / "workflows" / "branch-guardian.yml").read_text(encoding="utf-8")
        self.assertIn("BRANCH_GUARDIAN_BOOTSTRAPPED", workflow_text)
        self.assertIn("--auto --merge", workflow_text)


if __name__ == "__main__":
    unittest.main()
