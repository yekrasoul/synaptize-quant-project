import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.artifacts import ArtifactStore
from btc_dca_bridge.production_evidence import AUTHORIZATION, ProductionEvidenceService, preauthorization_status, sanitize_evidence
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
        sanitized = sanitize_evidence({"api_key": "key", "api_secret": "secret", "headers": {"authorization": "Bearer x"}, "safe": "ok"})
        self.assertEqual(sanitized, {"safe": "ok", "headers": {}})

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

    def test_cli_entrypoints_are_read_only_and_machine_readable(self):
        class Receipt:
            sha256 = "a" * 64
            path = self.root / "evidence.json"
        class FakeService:
            def __init__(self, **kwargs): pass
            def collect(self): return ({"status": "EVIDENCE_COMPLETE_NOT_READY", "evidence_id": "evidence-" + "a" * 32, "real_money_authorization": AUTHORIZATION}, Receipt())
            def verify(self, evidence_id, **kwargs): return {"status": "VALID_NOT_READY", "evidence_id": evidence_id}
        output = StringIO()
        with patch("btc_dca_bridge.cli.ProductionEvidenceService", FakeService), redirect_stdout(output):
            self.assertEqual(main(["collect-production-evidence", "--data-root", str(self.root / "data"), "--ledger", str(self.root / "ledger.jsonl"), "--json"]), 0)
            self.assertEqual(main(["verify-production-evidence", "evidence-" + "a" * 32, "--data-root", str(self.root / "data"), "--ledger", str(self.root / "ledger.jsonl"), "--json"]), 0)
        self.assertIn("EVIDENCE_COMPLETE_NOT_READY", output.getvalue())
        self.assertIn("VALID_NOT_READY", output.getvalue())


if __name__ == "__main__":
    unittest.main()
