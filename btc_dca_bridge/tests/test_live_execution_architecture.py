import unittest
from pathlib import Path

from btc_dca_bridge.artifacts.store import _DIRECTORIES
from btc_dca_bridge.canary import CANARY_EXPIRY
from btc_dca_bridge.live_activation import ACTIVATION_TTL
from btc_dca_bridge.live_readiness import APPROVAL_TTL
from btc_dca_bridge.real_money_authorization import AUTHORIZATION_TTL
from btc_dca_bridge.readiness_checks import ReadinessCheckId, project_gate_d_evidence
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

