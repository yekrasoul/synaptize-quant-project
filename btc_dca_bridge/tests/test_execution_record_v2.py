from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType
from btc_dca_bridge.core.models import Asset, Market, OrderIntent, OrderType, Side
from btc_dca_bridge.core.state import ExecutionState, Order
from btc_dca_bridge.infrastructure.execution_repository import JsonExecutionRepository
from btc_dca_bridge.schemas import validate_artifact


class ExecutionRecordV2Tests(unittest.TestCase):
    def test_schema_projection_has_lifecycle_evidence_but_no_secret_fields(self):
        intent = OrderIntent("intent-v2", "adaptive_dca_v1", "decision-v2", Market(Asset("BTC"), Asset("USDT")), Side.BUY, OrderType.MARKET, Decimal("25"), "bybit", "client-v2", "2026-10-24T00:00:00Z")
        order = Order(intent).transition(ExecutionState.RISK_APPROVED).transition(ExecutionState.SUBMISSION_STARTED).transition(ExecutionState.AMBIGUOUS, reason="timeout")
        record = JsonExecutionRepository.execution_record(order, account_fingerprint="safe-fingerprint", reconciliation_reference="client-v2")
        validate_artifact("execution_record_v2", record)
        self.assertFalse({"api_key", "api_secret", "signature", "headers"} & set(record))
        with TemporaryDirectory() as root:
            receipt = ArtifactStore(Path(root)).persist(ArtifactType.EXECUTION_RECORD_V2, record, run_id="run_20261024T000000Z_recordv2")
            self.assertTrue(receipt.path.exists())


if __name__ == "__main__": unittest.main()
