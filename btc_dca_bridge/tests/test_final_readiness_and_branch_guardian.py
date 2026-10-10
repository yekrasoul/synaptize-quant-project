import tempfile
import unittest
from pathlib import Path
import yaml

from btc_dca_bridge.branch_guardian import canonical_validation_succeeds, changed_diff_lines, classify_branch
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
        for path in (".github/workflows/branch-guardian.yml", ".github/workflows/branch-guardian-trusted.yml", "btc_dca_bridge/src/btc_dca_bridge/branch_guardian.py"):
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

    def test_canonical_check_requires_exact_name_sha_success_and_actions_app(self):
        sha = "h" * 40
        good = {"check_runs": [{"name": "Canonical validation", "conclusion": "success", "head_sha": sha, "app": {"slug": "github-actions"}}]}
        self.assertTrue(canonical_validation_succeeds(good, sha))
        for bad in (
            {"check_runs": []},
            {"check_runs": [{"name": "Canonical validation", "conclusion": "failure", "head_sha": sha, "app": {"slug": "github-actions"}}]},
            {"check_runs": [{"name": "Canonical validation", "conclusion": "success", "head_sha": "x" * 40, "app": {"slug": "github-actions"}}]},
            {"check_runs": [{"name": "Canonical validation", "conclusion": "success", "head_sha": sha, "app": {"slug": "other-app"}}]},
        ):
            self.assertFalse(canonical_validation_succeeds(bad, sha))

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
        self.assertNotIn("guardian", workflow["jobs"])
        trusted = yaml.safe_load((Path(__file__).parents[2] / ".github" / "workflows" / "branch-guardian-trusted.yml").read_text(encoding="utf-8"))
        self.assertEqual(trusted["jobs"]["trusted-guardian"]["name"], "Branch Guardian / Safety classification")
        self.assertEqual(trusted["jobs"]["trusted-guardian"]["permissions"]["pull-requests"], "write")
        self.assertEqual(trusted["jobs"]["trusted-guardian"]["permissions"]["contents"], "write")
        self.assertEqual(trusted["jobs"]["trusted-guardian"]["permissions"]["actions"], "read")
        self.assertEqual(trusted["jobs"]["trusted-guardian"]["permissions"]["checks"], "read")
        self.assertNotIn("pull-requests", workflow["permissions"])
        workflow_text = (Path(__file__).parents[2] / ".github" / "workflows" / "branch-guardian.yml").read_text(encoding="utf-8")
        self.assertIn("BRANCH_GUARDIAN_BOOTSTRAPPED", workflow_text)
        self.assertNotIn("gh pr merge", workflow_text)
        trusted_text = (Path(__file__).parents[2] / ".github" / "workflows" / "branch-guardian-trusted.yml").read_text(encoding="utf-8")
        self.assertIn('name == "Canonical validation"', trusted_text)
        self.assertIn("--auto --merge", trusted_text)
        self.assertNotIn("download-artifact", trusted_text)
        self.assertIn("workflow_run:", trusted_text)
        self.assertIn("conclusion == 'success'", trusted_text)
        self.assertNotIn("auto_merge_eligible", trusted_text)
        self.assertIn("headRefOid", trusted_text)
        self.assertIn("behind_by", trusted_text)
        self.assertIn("HEAD_SHA", trusted_text)
        for runtime_state in ("NOT_AUTHORIZED", "NOT_ACTIVATED", "NOT_ENABLED", "BLOCKED"):
            self.assertIn(runtime_state, trusted_text)
        ci = yaml.safe_load((Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        ci_triggers = ci.get("on", ci.get(True))
        self.assertEqual(set(ci_triggers["push"]["branches"]), {"feat/**", "fix/**", "refactor/**"})


if __name__ == "__main__":
    unittest.main()
