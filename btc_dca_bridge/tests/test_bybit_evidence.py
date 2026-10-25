from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType, make_run_id
from btc_dca_bridge.bybit_evidence import BybitEvidenceReadPort, capture
from btc_dca_bridge.errors import ArtifactSchemaValidationError
from btc_dca_bridge.private_bybit import AccountInfo, ApiCredentialInfo, CredentialClassification, WalletBalance
from btc_dca_bridge.schemas import validate_artifact


class ReadOnlyFixture:
    def credential_info(self): return ApiCredentialInfo(CredentialClassification.TRADE_CAPABLE, False, {"Spot": ("SpotTrade",)})
    def account_info(self): return AccountInfo("6", "REGULAR_MARGIN", "OFF", "now")
    def wallet_balances(self): return (WalletBalance("USDT", Decimal("12"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("12")),)
    def server_time_ms(self): return int(datetime.now(UTC).timestamp() * 1000)


class BybitEvidenceTests(unittest.TestCase):
    def test_capture_is_sanitized_and_schema_valid(self):
        payload = capture(ReadOnlyFixture(), environment="testnet")
        validate_artifact("bybit_account_evidence", payload)
        self.assertEqual((payload["quote_asset"], payload["quote_balance"], payload["withdrawal_permission_status"]), ("USDT", "12", "UNKNOWN"))
        forbidden = {"api_key", "api_secret", "secret", "signature", "headers", "raw_response"}
        self.assertFalse(forbidden & set(payload))

    def test_operator_environment_label_does_not_prove_authenticated_identity(self):
        payload = capture(ReadOnlyFixture(), environment="production")
        self.assertEqual(payload["environment"], "production")
        self.assertEqual(payload["withdrawal_permission_status"], "UNKNOWN")
        self.assertNotIn("account_environment_identity", payload)

    def test_capture_port_has_only_the_four_read_operations(self):
        methods = {name for name, value in BybitEvidenceReadPort.__dict__.items() if callable(value) and not name.startswith("_")}
        self.assertEqual(methods, {"credential_info", "account_info", "wallet_balances", "server_time_ms"})
        self.assertFalse(any(hasattr(ReadOnlyFixture(), name) for name in ("submit_once", "create_order", "amend_order", "cancel_order", "withdraw", "transfer")))

    def test_persistence_uses_canonical_artifact_and_never_serializes_unknown_permission_values(self):
        fixture = ReadOnlyFixture()
        fixture.credential_info = lambda: ApiCredentialInfo(
            CredentialClassification.INVALID, False, {"Authorization": ("api-secret-value",)}
        )
        payload = capture(
            fixture,
            environment="testnet",
            observed_at=datetime(2026, 10, 24, tzinfo=UTC),
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            store = ArtifactStore(root)
            receipt = store.persist(
                ArtifactType.BYBIT_ACCOUNT_EVIDENCE,
                payload,
                run_id=make_run_id(datetime(2026, 10, 24, tzinfo=UTC), "bybitevidence"),
            )
            serialized = receipt.path.read_text(encoding="utf-8")
            self.assertNotIn("api-secret-value", serialized)
            self.assertNotIn("api_key", serialized)
            self.assertNotIn("signature", serialized)
            self.assertEqual(
                store.read(
                    ArtifactType.BYBIT_ACCOUNT_EVIDENCE,
                    run_id=receipt.run_id,
                    artifact_date_utc=datetime(2026, 10, 24, tzinfo=UTC),
                ),
                payload,
            )

    def test_schema_validation_precedes_evidence_persistence(self):
        payload = capture(ReadOnlyFixture(), environment="testnet")
        del payload["account_fingerprint"]
        with TemporaryDirectory() as temporary:
            store = ArtifactStore(Path(temporary) / "data")
            with self.assertRaises(ArtifactSchemaValidationError):
                store.persist(ArtifactType.BYBIT_ACCOUNT_EVIDENCE, payload, run_id="run_20261024T000000Z_bybitevidence")

    def test_stale_exchange_time_is_explicitly_not_verified(self):
        payload = capture(
            ReadOnlyFixture(),
            environment="testnet",
            observed_at=datetime(2026, 10, 24, tzinfo=UTC),
        )
        self.assertEqual(payload["freshness_status"], "NOT_VERIFIED")


if __name__ == "__main__": unittest.main()
