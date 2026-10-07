import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType, make_run_id
from btc_dca_bridge.errors import (
    ArtifactAlreadyExistsError,
    ArtifactCorruptError,
    ArtifactNotFoundError,
    ArtifactSchemaValidationError,
    InvalidArtifactPathError,
    PersistenceIOError,
)
from btc_dca_bridge.paths import LEDGER_PATH
from btc_dca_bridge.execution import OrderIntent, SafetyValidationResult, client_order_id
from decimal import Decimal


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
RUN_ID = make_run_id(NOW, "a1b2c3d4e5f6")


def market():
    return {
        "schema_version": "1.0.0", "snapshot_id": "market_abc123",
        "captured_at_utc": "2026-10-07T12:00:00Z", "source_exchange": "Bybit",
        "market_type": "spot", "symbol": "BTCUSDT", "current_price_usdt": 80000,
        "rolling_7d_high_usdt": 90000, "window_start_utc": "2026-09-30T12:00:00Z",
        "window_end_utc": "2026-10-07T12:00:00Z",
        "validation": {"same_source_price_and_high": True, "full_168h_coverage": True, "fresh": True},
        "market_data_metadata": {"observation_resolution_seconds": 3600, "trade_level_exact": False},
    }


def sentiment():
    return {
        "schema_version": "1.0.0", "snapshot_id": "sentiment_abc123",
        "source": "alternative_me_crypto_fear_greed", "index_name": "Crypto Fear & Greed Index",
        "value": 35, "classification": "Fear", "observed_at_utc": "2026-10-07T00:00:00Z",
        "retrieved_at_utc": "2026-10-07T12:00:00Z",
        "upstream_identifier": "https://api.alternative.me/fng/?limit=1&format=json",
        "validation": {"value_in_range": True, "fresh": True, "source_identity_valid": True},
    }


def decision():
    return {
        "schema_version": "1.0.0", "decision_id": "decision_abc123",
        "created_at_utc": "2026-10-07T12:00:00Z", "strategy_id": "btc_adaptive_dca_v1",
        "strategy_version": "1.0.0", "market_snapshot_id": "market_abc123",
        "drawdown_percent": -11.11, "fear_greed_index": 35, "base_allocation_usd": 50,
        "sentiment_multiplier": 1.3, "calculated_allocation_usd": 65,
        "monthly_spent_before_usd": 0, "remaining_budget_before_usd": 500,
        "final_purchase_usd": 65, "status": "approved", "reason": "test",
    }


class LinkFailureStore(ArtifactStore):
    def __init__(self, root):
        super().__init__(root)
        self.calls = 0

    def _link_no_clobber(self, source, destination):
        self.calls += 1
        if self.calls == 2:
            raise OSError("simulated final publication failure")
        return super()._link_no_clobber(source, destination)


class ArtifactStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.store = ArtifactStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_persists_each_canonical_type_to_utc_layout_with_shared_run_id(self):
        expected = {
            ArtifactType.MARKET: market(), ArtifactType.SENTIMENT: sentiment(), ArtifactType.DECISION: decision(),
        }
        for kind, payload in expected.items():
            with self.subTest(kind=kind):
                receipt = self.store.persist(kind, payload, run_id=RUN_ID)
                self.assertEqual(receipt.run_id, RUN_ID)
                self.assertTrue(receipt.created)
                self.assertEqual(receipt.schema_version, "1.0.0")
                self.assertEqual(receipt.path.parent.relative_to(self.root).parts[-3:], ("2026", "10", "07"))
                self.assertTrue(receipt.path.is_file())
                self.assertTrue(receipt.path.with_suffix(".json.sha256").is_file())
                self.assertEqual(self.store.read(kind, run_id=RUN_ID, artifact_date_utc=NOW), payload)

    def test_canonical_bytes_digest_and_receipt_are_deterministic(self):
        receipt = self.store.persist("market", market(), run_id=RUN_ID)
        content = receipt.path.read_bytes()
        self.assertEqual(content, b'{"captured_at_utc":"2026-10-07T12:00:00Z","current_price_usdt":80000,"market_data_metadata":{"observation_resolution_seconds":3600,"trade_level_exact":false},"market_type":"spot","rolling_7d_high_usdt":90000,"schema_version":"1.0.0","snapshot_id":"market_abc123","source_exchange":"Bybit","symbol":"BTCUSDT","validation":{"fresh":true,"full_168h_coverage":true,"same_source_price_and_high":true},"window_end_utc":"2026-10-07T12:00:00Z","window_start_utc":"2026-09-30T12:00:00Z"}\n')
        self.assertEqual(receipt.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(receipt.byte_length, len(content))
        with self.assertRaises(ArtifactAlreadyExistsError):
            self.store.persist("market", market(), run_id=RUN_ID)

    def test_different_content_cannot_replace_existing_run_artifact(self):
        self.store.persist("market", market(), run_id=RUN_ID)
        changed = market()
        changed["current_price_usdt"] = 79999
        with self.assertRaises(ArtifactAlreadyExistsError):
            self.store.persist("market", changed, run_id=RUN_ID)
        self.assertEqual(self.store.read("market", run_id=RUN_ID, artifact_date_utc=NOW), market())

    def test_invalid_paths_and_missing_artifacts_fail_closed(self):
        for invalid in ("", "../run_20261007T120000Z_a1b2c3d4", "run_20261007T120000Z_short", "run_20261007T120000Z_a1b2/c3d4"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(InvalidArtifactPathError):
                    self.store.persist("market", market(), run_id=invalid)
        with self.assertRaises(ArtifactNotFoundError):
            self.store.read("market", run_id=RUN_ID, artifact_date_utc=NOW)

    def test_schema_validation_happens_before_any_final_or_temp_write(self):
        invalid = market()
        invalid["current_price_usdt"] = -1
        with self.assertRaises(ArtifactSchemaValidationError):
            self.store.persist("market", invalid, run_id=RUN_ID)
        self.assertFalse(list(self.root.rglob("*.json")) if self.root.exists() else [])
        self.assertFalse(list(self.root.rglob("*.tmp")) if self.root.exists() else [])

    def test_digest_json_schema_and_directory_corruption_are_rejected(self):
        receipt = self.store.persist("market", market(), run_id=RUN_ID)
        receipt.path.write_bytes(b"not json\n")
        with self.assertRaises(ArtifactCorruptError):
            self.store.read("market", run_id=RUN_ID, artifact_date_utc=NOW)

        receipt = self.store.persist("market", market(), run_id=make_run_id(NOW, "b1b2c3d4e5f6"))
        invalid = market()
        invalid["current_price_usdt"] = -1
        content = (json.dumps(invalid, sort_keys=True, separators=(",", ":")) + "\n").encode()
        receipt.path.write_bytes(content)
        receipt.path.with_suffix(".json.sha256").write_text(hashlib.sha256(content).hexdigest() + "\n")
        with self.assertRaises(ArtifactCorruptError):
            self.store.read("market", run_id=make_run_id(NOW, "b1b2c3d4e5f6"), artifact_date_utc=NOW)

    def test_missing_digest_and_wrong_utc_directory_are_corruption(self):
        receipt = self.store.persist("market", market(), run_id=RUN_ID)
        receipt.path.with_suffix(".json.sha256").unlink()
        with self.assertRaises(ArtifactCorruptError):
            self.store.read("market", run_id=RUN_ID, artifact_date_utc=NOW)

        other_id = make_run_id(NOW, "c1b2c3d4e5f6")
        receipt = self.store.persist("market", market(), run_id=other_id)
        wrong_date = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
        wrong_dir = self.root / "market" / "2026" / "10" / "08"
        wrong_dir.mkdir(parents=True)
        (wrong_dir / receipt.path.name).write_bytes(receipt.path.read_bytes())
        (wrong_dir / receipt.path.with_suffix(".json.sha256").name).write_bytes(receipt.path.with_suffix(".json.sha256").read_bytes())
        with self.assertRaises(ArtifactCorruptError):
            self.store.read("market", run_id=other_id, artifact_date_utc=wrong_date)

    def test_link_failure_leaves_no_final_or_temporary_artifact(self):
        failing = LinkFailureStore(self.root)
        with self.assertRaises(PersistenceIOError):
            failing.persist("market", market(), run_id=RUN_ID)
        self.assertFalse(list(self.root.rglob("*.json")))
        self.assertFalse(list(self.root.rglob("*.sha256")))
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_two_simultaneous_writers_have_one_winner_and_no_overwrite(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.store.persist, "market", market(), run_id=RUN_ID) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except ArtifactAlreadyExistsError:
                outcomes.append("exists")
        self.assertEqual(sum(outcome == "exists" for outcome in outcomes), 1)
        self.assertEqual(sum(outcome != "exists" for outcome in outcomes), 1)
        self.assertEqual(self.store.read("market", run_id=RUN_ID, artifact_date_utc=NOW), market())

    def test_persistence_never_mutates_ledger_or_calculates_a_decision(self):
        before = LEDGER_PATH.read_bytes()
        self.store.persist("decision", decision(), run_id=RUN_ID)
        self.assertEqual(LEDGER_PATH.read_bytes(), before)
        self.assertFalse(hasattr(self.store, "calculate_decision"))

    def test_execution_plan_evidence_is_immutable_and_not_execution(self):
        intent = OrderIntent("5.1.0", "btc_adaptive_dca_v1", "1.0.0", RUN_ID, "decision_abc123", "intent_abc123", "2026-10-07T12:00:00Z", "Bybit", "spot", "BTCUSDT", "Buy", Decimal("10"), "recommendation_only", client_order_id("btc_adaptive_dca_v1", "decision_abc123", RUN_ID), Decimal("0"), Decimal("500"), "pending", False, False)
        validation = SafetyValidationResult("5.1.0", RUN_ID, "rejected", ("kill switch is active",), "2026-10-07T12:00:01Z", Decimal("0"), Decimal("500"))
        intent_receipt = self.store.persist(ArtifactType.ORDER_INTENT, intent, run_id=RUN_ID)
        safety_receipt = self.store.persist(ArtifactType.SAFETY_VALIDATION, validation, run_id=RUN_ID)
        self.assertEqual(hashlib.sha256(intent_receipt.path.read_bytes()).hexdigest(), intent_receipt.path.with_suffix(".json.sha256").read_text().strip())
        self.assertEqual(hashlib.sha256(safety_receipt.path.read_bytes()).hexdigest(), safety_receipt.path.with_suffix(".json.sha256").read_text().strip())
        with self.assertRaises(ArtifactAlreadyExistsError): self.store.persist(ArtifactType.ORDER_INTENT, intent, run_id=RUN_ID)
        self.assertFalse(list((self.root / "executions").rglob("*")) if (self.root / "executions").exists() else [])
        self.assertFalse(list((self.root / "fills").rglob("*")) if (self.root / "fills").exists() else [])
        self.assertFalse(list((self.root / "order_submissions").rglob("*")) if (self.root / "order_submissions").exists() else [])


if __name__ == "__main__":
    unittest.main()
