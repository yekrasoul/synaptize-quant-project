import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.artifacts import ArtifactStore
from btc_dca_bridge.production_evidence import AUTHORIZATION, ProductionEvidenceService, preauthorization_status, sanitize_evidence, _account_fingerprint
from btc_dca_bridge.cli import main


COMMIT = "a" * 40


def readiness(status="NOT_READY", availability="FAIL"):
    def item(check_id, value):
        return {"check_id": check_id, "category": "test", "status": value, "required": True, "evidence": "safe", "reason": "test", "remediation": "test"}
    checks = [item("BYBIT_SPOT_AVAILABLE_BALANCE", availability), item("BYBIT_INSTRUMENT", "UNAVAILABLE"), item("CLOCK_SKEW", "PASS"), item("BYBIT_ACCOUNT", "PASS"), item("BYBIT_CREDENTIAL_SCOPE", "PASS"), item("BYBIT_LIABILITIES", "PASS"), item("MONTHLY_BUDGET", "PASS"), item("UNRESOLVED_OPERATIONS", "PASS"), item("SECRET_HYGIENE", "PASS"), item("FILESYSTEM_DURABILITY", "PASS"), item("OPERATOR_LOCK", "PASS")]
    return {"status": status, "checks": checks}


class FakeClient:
    class Credential:
        identity = "safe-non-secret-id"
    class Account:
        unified_margin_status = 6
        margin_mode = "REGULAR_MARGIN"
        spot_hedging_status = "OFF"
    def credential_info(self): return self.Credential()
    def account_info(self): return self.Account()


class ProductionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.now = datetime(2026, 10, 7, 12, tzinfo=UTC)
        self.repo = lambda: {"commit": COMMIT, "dirty": False}
        self.connectivity = lambda: {"status": "READS_OK", "read_only": True, "endpoints": [{"endpoint": "account_info", "status": "PASS", "read_only": True}], "message": "NO ORDER SUBMITTED"}
        self.service = ProductionEvidenceService(data_root=self.root / "data", ledger_path=self.root / "ledger.jsonl", now=lambda: self.now, client_factory=lambda: FakeClient(), readiness_factory=lambda **kwargs: type("Readiness", (), {"evaluate": lambda self: readiness()})(), connectivity_probe=self.connectivity, repo_probe=self.repo)

    def test_collect_persists_complete_not_ready_bundle_and_never_authorizes(self):
        bundle, receipt = self.service.collect()
        self.assertEqual(bundle["status"], "EVIDENCE_COMPLETE_NOT_READY")
        self.assertEqual(bundle["real_money_authorization"], AUTHORIZATION)
        self.assertTrue(receipt.path.name.startswith("evidence-"))
        self.assertEqual(self.service.verify(bundle["evidence_id"])["status"], "VALID_NOT_READY")

    def test_sanitizer_removes_secret_material(self):
        sanitized = sanitize_evidence({"X-BAPI-API-KEY": "key", "X_BAPI_API_KEY": "key2", "X-BAPI-SIGN": "sig", "Authorization": "Bearer x", "Bearer": "x", "api_secret": "secret", "apiKey": "key3", "TELEGRAM_BOT_TOKEN": "tg", "headers": {"authorization": "Bearer x", "nested": {"X-BAPI-SIGN": "sig"}}, "safe": "ok"})
        self.assertEqual(sanitized, {"safe": "ok", "headers": {}})

    def test_latest_uses_full_date_tree_and_creation_timestamp(self):
        first, _ = self.service.collect()
        self.now = datetime(2026, 10, 8, 12, tzinfo=UTC)
        second, _ = self.service.collect()
        latest = self.service.latest()
        self.assertEqual(latest["evidence_id"], second["evidence_id"])
        self.assertEqual(preauthorization_status(self.service)["evidence_id"], second["evidence_id"])
        self.assertNotEqual(first["evidence_id"], second["evidence_id"])

    def test_proven_account_identity_is_required_and_bound(self):
        bundle, _ = self.service.collect()
        fingerprint, status = _account_fingerprint(FakeClient())
        self.assertEqual(status, "PROVEN")
        self.assertEqual(self.service.verify(bundle["evidence_id"], current_account_fingerprint=fingerprint)["status"], "VALID_NOT_READY")
        self.assertEqual(self.service.verify(bundle["evidence_id"], current_account_fingerprint="b" * 64)["status"], "INVALID")
        unavailable = ProductionEvidenceService(data_root=self.root / "data", ledger_path=self.root / "ledger.jsonl", now=lambda: self.now, repo_probe=self.repo)
        self.assertEqual(unavailable.verify(bundle["evidence_id"], current_account_fingerprint=None)["status"], "INVALID")

    def test_legacy_57_bundle_is_integrity_valid_but_not_modern_ready(self):
        bundle, _ = self.service.collect()
        legacy = dict(bundle)
        legacy.pop("quote_unit_limit_result")
        legacy["schema_version"] = "5.7.0"
        legacy["evidence_id"] = "evidence-" + "c" * 32
        legacy["status"] = "EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION"
        receipt = ArtifactStore(self.root / "data").persist_production_evidence(legacy)
        self.assertEqual(receipt.schema_version, "5.7.0")
        result = self.service.verify(legacy["evidence_id"], current_account_fingerprint=_account_fingerprint(FakeClient())[0])
        self.assertEqual(result["status"], "VALID_NOT_READY")
        self.assertNotEqual(result["status"], "CORRUPT")
        self.assertIn("legacy evidence predates Phase 5.8 quote-unit-limit proof", result["reason"])

    def test_legacy_ready_bundle_blocks_preauthorization(self):
        bundle, _ = self.service.collect()
        original = self.root / "data" / "production_evidence" / "2026" / "10" / "07" / f"{bundle['evidence_id']}.json"
        original.unlink()
        original.with_suffix(original.suffix + ".sha256").unlink()
        legacy = dict(bundle)
        legacy.pop("quote_unit_limit_result")
        legacy.update({"schema_version": "5.7.0", "status": "EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION", "evidence_id": "evidence-" + "f" * 32})
        ArtifactStore(self.root / "data").persist_production_evidence(legacy)
        result = preauthorization_status(self.service)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertNotEqual(result["status"], "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION")

    def test_58_requires_quote_unit_limit_result_and_unknown_version_is_explicit(self):
        bundle, _ = self.service.collect()
        invalid = dict(bundle)
        invalid.pop("quote_unit_limit_result")
        invalid["evidence_id"] = "evidence-" + "d" * 32
        with self.assertRaises(Exception):
            ArtifactStore(self.root / "data").persist_production_evidence(invalid)
        unknown = dict(bundle, schema_version="9.9.9", evidence_id="evidence-" + "e" * 32)
        with self.assertRaises(Exception):
            ArtifactStore(self.root / "data").persist_production_evidence(unknown)

    def test_dynamic_ttl_boundaries_are_independent(self):
        bundle, _ = self.service.collect()
        self.now = datetime(2026, 10, 7, 12, 1, tzinfo=UTC)
        self.assertEqual(self.service.verify(bundle["evidence_id"], current_account_fingerprint=_account_fingerprint(FakeClient())[0])["status"], "VALID_NOT_READY")
        self.now = datetime(2026, 10, 7, 12, 1, 0, 1, tzinfo=UTC)
        self.assertEqual(self.service.verify(bundle["evidence_id"], current_account_fingerprint=_account_fingerprint(FakeClient())[0])["status"], "STALE")

    def test_unproven_account_identity_can_never_be_authorization_ready(self):
        bundle, _ = self.service.collect()
        path = self.root / "data" / "production_evidence" / "2026" / "10" / "07" / f"{bundle['evidence_id']}.json"
        changed = dict(bundle, account_identity_status="ACCOUNT_IDENTITY_UNPROVEN", account_identity_fingerprint=None, status="EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION")
        path.unlink()
        (path.with_suffix(path.suffix + ".sha256")).unlink()
        ArtifactStore(self.root / "data").persist_production_evidence(dict(changed, evidence_id="evidence-" + "b" * 32))
        result = self.service.verify("evidence-" + "b" * 32)
        self.assertEqual(result["status"], "VALID_NOT_READY")

    def test_cross_commit_host_and_expiry_invalidate(self):
        bundle, _ = self.service.collect()
        self.assertEqual(self.service.verify(bundle["evidence_id"], current_commit="b" * 40)["status"], "INVALID_FOR_CURRENT_BUILD")
        self.assertEqual(self.service.verify(bundle["evidence_id"], current_hostname="other-host")["status"], "INVALID")
        expired = dict(bundle, expires_at_utc="2026-10-07T12:00:00Z")
        path = self.root / "data" / "production_evidence" / "2026" / "10" / "07" / f"{bundle['evidence_id']}.json"
        path.write_text(json.dumps(expired), encoding="utf-8")
        self.assertEqual(self.service.verify(bundle["evidence_id"])["status"], "CORRUPT")

    def test_authorization_true_is_rejected_by_schema(self):
        bundle, _ = self.service.collect()
        invalid = dict(bundle, real_money_authorization={"granted": True, "source": "test", "required": True, "status": "AUTHORIZED"})
        with self.assertRaises(Exception):
            ArtifactStore(self.root / "data").persist_production_evidence(invalid)

    def test_preauthorization_never_returns_authorized(self):
        self.assertEqual(preauthorization_status(self.service)["status"], "BLOCKED")
        self.service.collect()
        result = preauthorization_status(self.service)
        self.assertNotEqual(result["status"], "AUTHORIZED")
        self.assertEqual(result["real_money_authorization"], AUTHORIZATION)
        self.assertEqual(result["quote_unit_limit"]["status"], "UNAVAILABLE")

    def test_cli_entrypoints_are_read_only_and_machine_readable(self):
        class Receipt:
            sha256 = "a" * 64
            path = self.root / "evidence.json"
        class FakeService:
            def __init__(self, **kwargs): pass
            def collect(self): return ({"status": "EVIDENCE_COMPLETE_NOT_READY", "evidence_id": "evidence-" + "a" * 32, "real_money_authorization": AUTHORIZATION}, Receipt())
            def verify(self, evidence_id, **kwargs): return {"status": "VALID_NOT_READY", "evidence_id": evidence_id}
        output = StringIO()
        with patch("btc_dca_bridge.cli.ProductionEvidenceService", FakeService), patch("btc_dca_bridge.cli._private_read_client_factory", FakeClient), redirect_stdout(output):
            self.assertEqual(main(["collect-production-evidence", "--data-root", str(self.root / "data"), "--ledger", str(self.root / "ledger.jsonl"), "--json"]), 0)
            self.assertEqual(main(["verify-production-evidence", "evidence-" + "a" * 32, "--data-root", str(self.root / "data"), "--ledger", str(self.root / "ledger.jsonl"), "--json"]), 0)
        self.assertIn("EVIDENCE_COMPLETE_NOT_READY", output.getvalue())
        self.assertIn("VALID_NOT_READY", output.getvalue())


if __name__ == "__main__":
    unittest.main()
