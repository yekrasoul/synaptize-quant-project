import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType
from btc_dca_bridge.config import ExecutionConfig, load_execution_config
from btc_dca_bridge.live_readiness import GateStatus, LiveReadinessError
from btc_dca_bridge.production_enablement import (
    ENABLEMENT_CONFIRMATION,
    ENABLEMENT_TTL,
    ProductionEnablement,
    gate_d,
    prepare_production_enablement,
    read_enablement_status,
)
from btc_dca_bridge.real_money_authorization import artifact_sha256


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
SHA = "a" * 64
COMMIT = "b" * 40


def evidence():
    return {key: True for key in (
        "canonical_ci_healthy", "ledger_valid", "v1_strategy_valid", "production_contract_valid",
        "account_identity_known", "account_mode_accepted", "liabilities_clear", "credential_scope_accepted",
        "withdrawal_disabled", "spot_availability_operational", "instrument_metadata_operational",
        "clock_operational", "reconciliation_operational", "production_shadow_healthy", "watchdog_healthy",
        "no_unresolved_submission", "no_reconciliation_required", "no_artifact_corruption",
    )} | {"monthly_cap_usd": 500, "account_identity_fingerprint": SHA, "account_mode": "UTA", "credential_scope": "SPOT_TRADE_NO_WITHDRAW", "production_contract_fingerprint": SHA}


def make_enablement(store):
    return prepare_production_enablement(
        store=store, evidence=evidence(), repository_commit_sha=COMMIT,
        execution_policy_fingerprint=SHA, production_contract_fingerprint=SHA,
        account_identity_fingerprint=SHA, account_mode="UTA", credential_scope="SPOT_TRADE_NO_WITHDRAW",
        confirmation=ENABLEMENT_CONFIRMATION, run_id="run_20261010T120000Z_enablement", now_utc=NOW,
    )


class ProductionEnablementTest(unittest.TestCase):
    def test_exact_ceremony_and_bounded_ttl(self):
        with tempfile.TemporaryDirectory() as raw:
            enablement, receipt = make_enablement(ArtifactStore(Path(raw)))
            self.assertEqual(enablement.payload["status"], "PREPARED")
            self.assertEqual(datetime.fromisoformat(enablement.payload["expires_at_utc"].replace("Z", "+00:00")) - NOW, ENABLEMENT_TTL)
            self.assertEqual(receipt.sha256, artifact_sha256(enablement.payload))
            with self.assertRaises(LiveReadinessError):
                ProductionEnablement.prepare(evidence=evidence(), repository_commit_sha=COMMIT, execution_policy_fingerprint=SHA, production_contract_fingerprint=SHA, account_identity_fingerprint=SHA, account_mode="UTA", credential_scope="SPOT_TRADE_NO_WITHDRAW", confirmation="go", now_utc=NOW)

    def test_prerequisite_unknown_fails_closed(self):
        broken = evidence(); broken["watchdog_healthy"] = False
        with self.assertRaises(LiveReadinessError):
            ProductionEnablement.prepare(evidence=broken, repository_commit_sha=COMMIT, execution_policy_fingerprint=SHA, production_contract_fingerprint=SHA, account_identity_fingerprint=SHA, account_mode="UTA", credential_scope="SPOT_TRADE_NO_WITHDRAW", confirmation=ENABLEMENT_CONFIRMATION, now_utc=NOW)

    def test_current_defaults_are_not_enabled_even_with_valid_artifact(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ArtifactStore(Path(raw)); enablement, receipt = make_enablement(store)
            result = gate_d(enablement=enablement.payload, enablement_sha256=receipt.sha256, runtime_config=load_execution_config(), store=store, repository_commit_sha=COMMIT, evidence=evidence(), now_utc=NOW)
            self.assertEqual(result.status, GateStatus.BLOCKED)
            self.assertIn("kill switch is active", result.reasons)
            self.assertIn("live execution is disabled", result.reasons)

    def test_future_enabled_fixture_can_pass_gate_d_without_network(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ArtifactStore(Path(raw)); enablement, receipt = make_enablement(store)
            config = ExecutionConfig("1.0.0", True, False, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "implemented")
            result = gate_d(enablement=enablement.payload, enablement_sha256=receipt.sha256, runtime_config=config, store=store, repository_commit_sha=COMMIT, evidence=evidence(), now_utc=NOW)
            self.assertEqual(result.status, GateStatus.READY_FOR_SUBMISSION)

    def test_status_is_read_only_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ArtifactStore(Path(raw))
            status = read_enablement_status(store=store)
            self.assertEqual(status["enablement"], "NOT_ENABLED")
            self.assertEqual(status["execution"], "BLOCKED")
            self.assertEqual(status["message"], "NO ORDER EXECUTED")
            before = sorted(Path(raw).rglob("*"))
            _ = read_enablement_status(store=store)
            self.assertEqual(before, sorted(Path(raw).rglob("*")))

    def test_valid_artifact_reports_prepared_without_exposing_governance_digest(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ArtifactStore(Path(raw)); enablement, _ = make_enablement(store)
            status = read_enablement_status(store=store, now_utc=NOW)
            self.assertEqual(status["enablement"], "PREPARED")
            self.assertNotIn("operator_confirmation_digest", status)
            self.assertNotIn("account_identity_fingerprint", status)

    def test_expired_and_revoked_are_not_active(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ArtifactStore(Path(raw)); enablement, receipt = make_enablement(store)
            self.assertEqual(read_enablement_status(store=store, now_utc=NOW + timedelta(days=2))["enablement"], "EXPIRED")
            revocation = {"schema_version": "5.4.0E-R", "revocation_id": "enablement-revocation-" + "c" * 32, "created_at_utc": "2026-10-10T12:01:00Z", "enablement_id": enablement.enablement_id, "enablement_sha256": receipt.sha256, "reason_digest": SHA, "no_order_executed": True}
            store.persist(ArtifactType.PRODUCTION_ENABLEMENT_REVOCATION, revocation, run_id="run_20261010T120100Z_revoke_record")
            self.assertEqual(read_enablement_status(store=store, now_utc=NOW)["enablement"], "REVOKED")


if __name__ == "__main__":
    unittest.main()
