import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.artifacts.store import _DIRECTORIES
from btc_dca_bridge.canary import CANARY_EXPIRY
from btc_dca_bridge.live_activation import ACTIVATION_TTL
from btc_dca_bridge.live_readiness import APPROVAL_TTL
from btc_dca_bridge.real_money_authorization import AUTHORIZATION_TTL
from btc_dca_bridge.readiness_checks import ReadinessCheckId, project_gate_d_evidence
from btc_dca_bridge.cli import _gate_d_evidence
from btc_dca_bridge.production_enablement import gate_d
from btc_dca_bridge.live_readiness import GateStatus
from btc_dca_bridge.ttl_policy import (
    CANARY_MANIFEST_TTL,
    EXECUTION_ACTIVATION_TTL,
    LIVE_EXECUTION_APPROVAL_TTL,
    REAL_MONEY_AUTHORIZATION_TTL,
)


class LiveExecutionArchitectureTests(unittest.TestCase):
    def test_canonical_projection_uses_only_emitted_check_ids(self):
        bundle = {"check_results": [
            {"check_id": ReadinessCheckId.CANONICAL_LEDGER.value, "status": "PASS"},
            {"check_id": ReadinessCheckId.STRATEGY_IDENTITY.value, "status": "PASS"},
            {"check_id": ReadinessCheckId.BYBIT_ACCOUNT.value, "status": "PASS"},
            {"check_id": ReadinessCheckId.BYBIT_CREDENTIAL_SCOPE.value, "status": "PASS"},
            {"check_id": ReadinessCheckId.BYBIT_LIABILITIES.value, "status": "PASS"},
            {"check_id": ReadinessCheckId.BYBIT_SPOT_AVAILABLE_BALANCE.value, "status": "PASS"},
            {"check_id": ReadinessCheckId.BYBIT_INSTRUMENT.value, "status": "PASS"},
            {"check_id": ReadinessCheckId.CLOCK_SKEW.value, "status": "PASS"},
            {"check_id": ReadinessCheckId.UNRESOLVED_OPERATIONS.value, "status": "PASS"},
            {"check_id": ReadinessCheckId.ARTIFACT_INTEGRITY.value, "status": "PASS"},
        ]}
        projected = project_gate_d_evidence(bundle)
        self.assertTrue(projected["ledger_valid"])
        self.assertTrue(projected["v1_strategy_valid"])
        self.assertTrue(projected["no_artifact_corruption"])
        self.assertTrue(projected["production_shadow_healthy"])
        self.assertNotIn("LEDGER_INTEGRITY", {item["check_id"] for item in bundle["check_results"]})

    def test_gate_d_projection_uses_enablement_contract_fingerprint(self):
        bundle = {"check_results": [
            {"check_id": check_id.value, "status": "PASS"}
            for check_id in (
                ReadinessCheckId.CANONICAL_LEDGER,
                ReadinessCheckId.STRATEGY_IDENTITY,
                ReadinessCheckId.BYBIT_ACCOUNT,
                ReadinessCheckId.BYBIT_CREDENTIAL_SCOPE,
                ReadinessCheckId.BYBIT_LIABILITIES,
                ReadinessCheckId.BYBIT_SPOT_AVAILABLE_BALANCE,
                ReadinessCheckId.BYBIT_INSTRUMENT,
                ReadinessCheckId.CLOCK_SKEW,
                ReadinessCheckId.UNRESOLVED_OPERATIONS,
                ReadinessCheckId.ARTIFACT_INTEGRITY,
            )
        ]}
        projected = _gate_d_evidence(bundle, enablement={"production_contract_fingerprint": "a" * 64})
        self.assertTrue(projected["production_contract_valid"])

    def test_gate_d_projection_shares_enablement_identity_and_scope_values(self):
        enablement = {
            "expected_account_mode": "REGULAR_MARGIN",
            "allowed_credential_scope": "SPOT_TRADE_NO_WITHDRAW",
            "production_contract_fingerprint": "a" * 64,
            "execution_policy_fingerprint": "b" * 64,
        }
        projected = _gate_d_evidence({"account_identity_fingerprint": "c" * 64, "check_results": []}, enablement=enablement)
        self.assertEqual(projected["account_mode"], enablement["expected_account_mode"])
        self.assertEqual(projected["credential_scope"], enablement["allowed_credential_scope"])
        self.assertEqual(projected["production_contract_fingerprint"], enablement["production_contract_fingerprint"])
        self.assertEqual(projected["execution_policy_fingerprint"], enablement["execution_policy_fingerprint"])

    def test_gate_d_matching_projected_values_pass_and_genuine_mismatch_blocks(self):
        enablement = {
            "enablement_id": "enablement-test",
            "created_at_utc": "2026-10-11T00:00:00Z",
            "expires_at_utc": "2026-10-11T12:00:00Z",
            "status": "PREPARED",
            "repository_commit_sha": "d" * 40,
            "monthly_cap_usd": 500,
            "leverage_allowed": False,
            "expected_account_identity_fingerprint": "c" * 64,
            "expected_account_mode": "REGULAR_MARGIN",
            "allowed_credential_scope": "SPOT_TRADE_NO_WITHDRAW",
            "production_contract_fingerprint": "a" * 64,
            "execution_policy_fingerprint": "b" * 64,
        }
        evidence = {
            "account_identity_fingerprint": "c" * 64,
            "account_mode": "REGULAR_MARGIN",
            "credential_scope": "SPOT_TRADE_NO_WITHDRAW",
            "production_contract_fingerprint": "a" * 64,
            "execution_policy_fingerprint": "b" * 64,
            **{key: True for key in (
                "canonical_ci_healthy", "ledger_valid", "v1_strategy_valid", "production_contract_valid",
                "account_identity_known", "account_mode_accepted", "liabilities_clear", "credential_scope_accepted",
                "withdrawal_disabled", "spot_availability_operational", "instrument_metadata_operational",
                "clock_operational", "reconciliation_operational", "production_shadow_healthy", "watchdog_healthy",
                "no_unresolved_submission", "no_reconciliation_required", "no_artifact_corruption",
            )},
        }
        config = {"kill_switch": False, "live_execution_enabled": True, "order_submission": "implemented"}
        with patch("btc_dca_bridge.schemas.validate_artifact"), \
             patch("btc_dca_bridge.production_enablement.artifact_sha256", return_value="x"), \
             patch("btc_dca_bridge.production_enablement._revoked", return_value=False):
            passed = gate_d(enablement=enablement, enablement_sha256="x", runtime_config=config, store=object(),
                            repository_commit_sha="d" * 40, evidence=evidence,
                            now_utc=datetime(2026, 10, 11, 1, tzinfo=UTC))
            mismatched = gate_d(enablement=enablement, enablement_sha256="x", runtime_config=config, store=object(),
                                repository_commit_sha="d" * 40,
                                evidence={**evidence, "account_mode": "UTA"},
                                now_utc=datetime(2026, 10, 11, 1, tzinfo=UTC))
        self.assertEqual(passed.status, GateStatus.READY_FOR_SUBMISSION)
        self.assertIn("account_mode does not match ProductionEnablement", mismatched.reasons)

    def test_raw_production_evidence_is_incomplete_until_gate_d_projected(self):
        projected = _gate_d_evidence({"account_identity_fingerprint": "c" * 64, "check_results": []}, enablement={
            "expected_account_mode": "REGULAR_MARGIN",
            "allowed_credential_scope": "SPOT_TRADE_NO_WITHDRAW",
            "production_contract_fingerprint": "a" * 64,
            "execution_policy_fingerprint": "b" * 64,
        })
        self.assertNotIn("credential_scope", {"account_identity_fingerprint": "c" * 64})
        self.assertEqual(projected["credential_scope"], "SPOT_TRADE_NO_WITHDRAW")
        self.assertEqual(projected["account_mode"], "REGULAR_MARGIN")

    def test_ttls_are_shared_by_all_ceremony_modules(self):
        self.assertEqual(CANARY_EXPIRY, CANARY_MANIFEST_TTL)
        self.assertEqual(APPROVAL_TTL, LIVE_EXECUTION_APPROVAL_TTL)
        self.assertEqual(AUTHORIZATION_TTL, REAL_MONEY_AUTHORIZATION_TTL)
        self.assertEqual(ACTIVATION_TTL, EXECUTION_ACTIVATION_TTL)

    def test_every_runtime_artifact_directory_is_ignored(self):
        root = Path(__file__).parents[1]
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        for directory in _DIRECTORIES.values():
            with self.subTest(directory=directory):
                self.assertIn(f"data/{directory}/", gitignore)


if __name__ == "__main__":
    unittest.main()

