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
    def test_old_approval_is_stale_for_current_repository(self):
        result = cli._assess_readiness_artifact(
            "live_execution_approval",
            {"approval_id": "approval5b-" + "a" * 32, "repository_commit_sha": "b" * 40,
             "created_at_utc": "2026-10-10T11:55:00Z", "expires_at_utc": "2026-10-10T12:05:00Z"},
            "c" * 64, current_commit=COMMIT, now=NOW,
            identity_key="approval_id", created_key="created_at_utc", ttl=timedelta(minutes=10),
        )
        self.assertEqual(result["status"], "STALE_REPOSITORY")

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


if __name__ == "__main__":
    unittest.main()
