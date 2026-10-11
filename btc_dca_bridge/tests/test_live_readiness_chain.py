import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge import cli
from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
COMMIT = "a" * 40


class LiveReadinessChainTests(unittest.TestCase):
    def ceremony_rows(self, *, authorization=None, activation=None, enablement=None):
        future = "2026-10-10T12:30:00Z"
        decision_sha = "d" * 64
        intent_sha = "i" * 64
        manifest_sha = "m" * 64
        approval_sha = "p" * 64
        authorization_sha = "a" * 64
        client = "dca-" + "1" * 32
        approval = {"approval_id": "approval5b-" + "1" * 32, "canary_manifest_id": "canary", "canary_manifest_sha256": manifest_sha,
                    "decision_id": "decision", "decision_sha256": decision_sha, "order_intent_id": "intent", "order_intent_sha256": intent_sha,
                    "client_order_id": client, "quote_amount_usdt": "10", "calendar_month": "2026-10", "one_shot_identity": "one",
                    "production_contract_fingerprint": "c" * 64, "execution_safety_config_fingerprint": "s" * 64,
                    "account_identity_fingerprint": "f" * 64, "repository_commit_sha": COMMIT,
                    "created_at_utc": "2026-10-10T12:20:00Z", "expires_at_utc": future}
        auth = {"authorization_id": "auth", "approval_id": approval["approval_id"], "approval_sha256": approval_sha,
                "canary_manifest_id": "canary", "canary_manifest_sha256": manifest_sha, "decision_id": "decision", "decision_sha256": decision_sha,
                "order_intent_id": "intent", "order_intent_sha256": intent_sha, "client_order_id": client,
                "exact_quote_amount_usdt": "10", "calendar_month": "2026-10", "one_shot_identity": "one",
                "repository_commit_sha": COMMIT, "created_at_utc": "2026-10-10T12:21:00Z", "expires_at_utc": future}
        act = {"activation_id": "activation", "authorization_id": "auth", "authorization_sha256": authorization_sha,
               "approval_id": approval["approval_id"], "approval_sha256": approval_sha, "canary_manifest_id": "canary", "canary_manifest_sha256": manifest_sha,
               "decision_id": "decision", "decision_sha256": decision_sha, "order_intent_id": "intent", "order_intent_sha256": intent_sha,
               "client_order_id": client, "exact_quote_amount_usdt": "10", "calendar_month": "2026-10", "one_shot_identity": "one",
               "repository_commit_sha": COMMIT, "created_at_utc": "2026-10-10T12:25:00Z", "expires_at_utc": future}
        enable = {"enablement_id": "enablement", "repository_commit_sha": COMMIT, "production_contract_fingerprint": "c" * 64,
                  "execution_policy_fingerprint": "s" * 64, "expected_account_identity_fingerprint": "f" * 64,
                  "created_at_utc": "2026-10-10T12:23:00Z", "expires_at_utc": future}
        rows = {
            "decision": [({"decision_id": "decision", "created_at_utc": "2026-10-10T11:00:00Z"}, decision_sha)],
            "order_intent": [({"order_intent_id": "intent", "decision_id": "decision", "client_order_id": client, "quote_amount_usd": "10", "calendar_month": "2026-10", "created_at_utc": "2026-10-10T11:01:00Z"}, intent_sha)],
            "canary_manifest": [({"canary_id": "canary", "order_intent_id": "intent", "client_order_id": client, "prepared_at_utc": "2026-10-10T12:19:00Z", "expires_at_utc": future}, manifest_sha)],
            "live_execution_approval": [(approval, approval_sha)],
            "real_money_authorization": [(authorization or auth, authorization_sha)],
            "execution_activation": [(activation or act, "x" * 64)],
            "production_enablement": [(enablement or enable, "e" * 64)],
        }
        return rows

    def resolve(self, rows):
        directories = dict(zip((item[0] for item in cli._READINESS_ARTIFACTS), (item[2] for item in cli._READINESS_ARTIFACTS)))
        with patch.object(cli, "_readiness_candidates", side_effect=lambda _s, _kind, directory: rows[next(label for label, value in directories.items() if value == directory)]):
            return cli._resolve_readiness_chain(object(), current_commit=COMMIT, now=NOW)
    def evidence_store(self, payloads):
        root = Path(tempfile.mkdtemp()) / "production_evidence" / "2026" / "10" / "10"
        root.mkdir(parents=True)
        for evidence_id in payloads:
            (root / f"{evidence_id}.json").write_text("{}\n", encoding="utf-8")
        class Store:
            def __init__(self, base, rows): self.root, self.rows = base, rows
            def read_production_evidence(self, evidence_id): return self.rows[evidence_id]
        return Store(root.parents[3], payloads)

    def evidence(self, evidence_id, *, commit=COMMIT, created="2026-10-10T11:00:00Z", expires="2026-10-10T12:30:00Z", status="EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION"):
        return {"evidence_id": evidence_id, "repository_commit": commit, "created_at_utc": created, "expires_at_utc": expires, "status": status}

    def test_newer_ready_with_smaller_id_beats_older_expired_evidence(self):
        rows = {"evidence-z": self.evidence("evidence-z", created="2026-10-10T10:00:00Z", expires="2026-10-10T11:00:00Z"), "evidence-a": self.evidence("evidence-a", created="2026-10-10T11:30:00Z")}
        selected = cli._select_production_evidence(self.evidence_store(rows), current_commit=COMMIT, now=NOW)[0]
        self.assertEqual(selected["evidence_id"], "evidence-a")

    def test_current_head_beats_foreign_commit(self):
        rows = {"evidence-z": self.evidence("evidence-z", commit="b" * 40, created="2026-10-10T12:00:00Z"), "evidence-a": self.evidence("evidence-a", created="2026-10-10T11:00:00Z")}
        selected = cli._select_production_evidence(self.evidence_store(rows), current_commit=COMMIT, now=NOW)[0]
        self.assertEqual(selected["evidence_id"], "evidence-a")

    def test_expired_and_not_ready_do_not_beat_ready(self):
        rows = {"evidence-a": self.evidence("evidence-a", created="2026-10-10T11:50:00Z", expires="2026-10-10T11:59:00Z"), "evidence-b": self.evidence("evidence-b", created="2026-10-10T11:55:00Z", status="EVIDENCE_COMPLETE_NOT_READY"), "evidence-c": self.evidence("evidence-c", created="2026-10-10T11:40:00Z")}
        selected = cli._select_production_evidence(self.evidence_store(rows), current_commit=COMMIT, now=NOW)[0]
        self.assertEqual(selected["evidence_id"], "evidence-c")

    def test_deterministic_selection_uses_newest_created_then_id(self):
        rows = {"evidence-b": self.evidence("evidence-b"), "evidence-a": self.evidence("evidence-a")}
        for _ in range(3):
            selected = cli._select_production_evidence(self.evidence_store(rows), current_commit=COMMIT, now=NOW)[0]
            self.assertEqual(selected["evidence_id"], "evidence-b")

    def test_old_approval_is_stale_for_current_repository(self):
        result = cli._assess_readiness_artifact(
            "live_execution_approval",
            {"approval_id": "approval5b-" + "a" * 32, "repository_commit_sha": "b" * 40,
             "created_at_utc": "2026-10-10T11:55:00Z", "expires_at_utc": "2026-10-10T12:05:00Z"},
            "c" * 64, current_commit=COMMIT, now=NOW,
            identity_key="approval_id", created_key="created_at_utc", ttl=timedelta(minutes=10),
        )
        self.assertEqual(result["status"], "STALE_REPOSITORY")

    def test_stale_authorization_does_not_override_matching_current_authorization(self):
        rows = self.ceremony_rows()
        stale = dict(rows["real_money_authorization"][0][0], repository_commit_sha="b" * 40, authorization_id="stale-auth")
        rows["real_money_authorization"].append((stale, "z" * 64))
        chain, blockers = self.resolve(rows)
        self.assertEqual(chain["real_money_authorization"]["id"], "auth")
        self.assertNotIn("real_money_authorization: STALE_REPOSITORY", blockers)

    def test_stale_activation_does_not_override_matching_current_activation(self):
        rows = self.ceremony_rows()
        stale = dict(rows["execution_activation"][0][0], repository_commit_sha="b" * 40, activation_id="stale-activation")
        rows["execution_activation"].append((stale, "z" * 64))
        chain, _ = self.resolve(rows)
        self.assertEqual(chain["execution_activation"]["id"], "activation")

    def test_stale_enablement_does_not_override_matching_current_enablement(self):
        rows = self.ceremony_rows()
        stale = dict(rows["production_enablement"][0][0], repository_commit_sha="b" * 40, enablement_id="stale-enablement")
        rows["production_enablement"].append((stale, "z" * 64))
        chain, _ = self.resolve(rows)
        self.assertEqual(chain["production_enablement"]["id"], "enablement")

    def test_coherent_current_chain_is_complete(self):
        chain, blockers = self.resolve(self.ceremony_rows())
        self.assertFalse(blockers)
        self.assertTrue(all(item["status"] == "VALID" for item in chain.values()))

    def test_artifacts_from_different_chains_are_not_combined(self):
        rows = self.ceremony_rows()
        rows["order_intent"] = [(dict(rows["order_intent"][0][0], order_intent_id="other-intent"), "o" * 64)]
        chain, blockers = self.resolve(rows)
        self.assertIn("BINDING_MISMATCH", " ".join(blockers))
        self.assertNotEqual(chain["live_execution_approval"]["status"], "VALID")

    def test_expired_current_activation_is_blocked(self):
        rows = self.ceremony_rows()
        expired = dict(rows["execution_activation"][0][0], expires_at_utc="2026-10-10T11:59:00Z", status="ACTIVE")
        rows["execution_activation"] = [(expired, "x" * 64)]
        chain, blockers = self.resolve(rows)
        self.assertEqual(chain["execution_activation"]["status"], "EXPIRED")
        self.assertIn("execution_activation: EXPIRED", blockers)

    def test_consumed_current_authorization_is_blocked(self):
        rows = self.ceremony_rows()
        consumed = dict(rows["real_money_authorization"][0][0], consumed=True, status="AUTHORIZED")
        rows["real_money_authorization"] = [(consumed, "a" * 64)]
        chain, blockers = self.resolve(rows)
        self.assertEqual(chain["real_money_authorization"]["status"], "CONSUMED")
        self.assertIn("real_money_authorization: CONSUMED", blockers)

    def test_expired_status_labels_are_not_trusted(self):
        for label, payload, identity, ttl in (
            ("real_money_authorization", {"authorization_id": "auth", "repository_commit_sha": COMMIT, "created_at_utc": "2026-10-10T11:00:00Z", "expires_at_utc": "2026-10-10T11:59:00Z", "status": "AUTHORIZED"}, "authorization_id", timedelta(minutes=10)),
            ("execution_activation", {"activation_id": "activation", "repository_commit_sha": COMMIT, "created_at_utc": "2026-10-10T11:00:00Z", "expires_at_utc": "2026-10-10T11:59:00Z", "status": "ACTIVE"}, "activation_id", timedelta(minutes=5)),
        ):
            result = cli._assess_readiness_artifact(label, payload, "d" * 64, current_commit=COMMIT, now=NOW, identity_key=identity, created_key="created_at_utc", ttl=ttl)
            self.assertEqual(result["status"], "EXPIRED")

    def test_latest_different_chain_is_not_combined(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ArtifactStore(Path(raw))
            with patch.object(cli, "_current_commit", return_value=COMMIT), patch.object(cli, "_readiness_latest", return_value=None):
                report, _ = cli._live_readiness(type("Args", (), {"data_root": Path(raw), "ledger": Path(raw) / "ledger"})())
            self.assertEqual(report["next_allowed_command"].split()[1], "collect-production-evidence")
            self.assertIn("production evidence: MISSING", report["blockers"])

    def test_current_coherent_upstream_chain_recommends_enablement(self):
        with tempfile.TemporaryDirectory() as raw:
            data_root = Path(raw)
            future = "2026-10-10T12:20:00Z"
            rows = {
                ArtifactType.DECISION: ({"decision_id": "d", "created_at_utc": "2026-10-10T11:00:00Z"}, "d" * 64),
                ArtifactType.ORDER_INTENT: ({"order_intent_id": "i", "decision_id": "d", "client_order_id": "dca-" + "a" * 32, "quote_amount_usd": "10", "created_at_utc": "2026-10-10T11:01:00Z"}, "i" * 64),
                ArtifactType.CANARY_MANIFEST: ({"canary_id": "c", "order_intent_id": "i", "client_order_id": "dca-" + "a" * 32, "prepared_at_utc": "2026-10-10T11:02:00Z", "expires_at_utc": future}, "m" * 64),
                ArtifactType.LIVE_EXECUTION_APPROVAL: ({"approval_id": "p", "canary_manifest_id": "c", "canary_manifest_sha256": "m" * 64, "order_intent_sha256": "i" * 64, "client_order_id": "dca-" + "a" * 32, "quote_amount_usdt": "10", "calendar_month": "2026-10", "one_shot_identity": "one", "repository_commit_sha": COMMIT, "created_at_utc": "2026-10-10T11:03:00Z", "expires_at_utc": future}, "p" * 64),
                ArtifactType.REAL_MONEY_AUTHORIZATION: ({"authorization_id": "a", "approval_id": "p", "approval_sha256": "p" * 64, "client_order_id": "dca-" + "a" * 32, "exact_quote_amount_usdt": "10", "calendar_month": "2026-10", "one_shot_identity": "one", "repository_commit_sha": COMMIT, "created_at_utc": "2026-10-10T11:04:00Z", "expires_at_utc": future}, "a" * 64),
                ArtifactType.EXECUTION_ACTIVATION: ({"activation_id": "x", "authorization_id": "a", "approval_sha256": "p" * 64, "authorization_sha256": "a" * 64, "client_order_id": "dca-" + "a" * 32, "exact_quote_amount_usdt": "10", "calendar_month": "2026-10", "one_shot_identity": "one", "repository_commit_sha": COMMIT, "created_at_utc": "2026-10-10T11:05:00Z", "expires_at_utc": future}, "x" * 64),
                ArtifactType.PRODUCTION_EVIDENCE: ({"evidence_id": "e", "repository_commit": COMMIT, "status": "EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION", "expires_at_utc": future}, "e" * 64),
            }
            dirs = {ArtifactType.DECISION: "decisions", ArtifactType.ORDER_INTENT: "order_intents", ArtifactType.CANARY_MANIFEST: "canary_manifests", ArtifactType.LIVE_EXECUTION_APPROVAL: "live_execution_approvals", ArtifactType.REAL_MONEY_AUTHORIZATION: "real_money_authorizations", ArtifactType.EXECUTION_ACTIVATION: "execution_activations"}
            with patch.object(cli, "_current_commit", return_value=COMMIT), patch.object(cli, "_readiness_latest", side_effect=lambda _s, kind, _d: rows.get(kind)):
                report, _ = cli._live_readiness(type("Args", (), {"data_root": data_root, "ledger": data_root / "ledger"})())
            self.assertEqual(report["next_allowed_command"], "btc-dca collect-production-evidence --data-root data --ledger ledger/executions.jsonl")

    def test_readiness_distinguishes_checked_in_defaults_from_ephemeral_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(cli, "_current_commit", return_value=COMMIT), patch.object(cli, "_readiness_latest", return_value=None):
                report, _ = cli._live_readiness(type("Args", (), {"data_root": Path(raw), "ledger": Path(raw) / "ledger"})())
            self.assertEqual(report["safety"]["checked_in"], {
                "live_execution_enabled": False,
                "kill_switch": True,
                "order_submission": "not_implemented",
                "monthly_cap_usd": "500",
            })
            self.assertEqual(report["safety"]["one_shot_runtime"], {
                "live_execution_enabled": True,
                "kill_switch": False,
                "order_submission": "implemented",
                "mode": "EPHEMERAL_ONLY_AFTER_ALL_GATES",
            })


if __name__ == "__main__":
    unittest.main()
