import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.blocked_production import (
    QUOTE_LIMIT_BLOCKER_ID,
    active_production_blockers,
    compare_contract_capabilities,
    contract_status,
    current_contract_capabilities,
)
from btc_dca_bridge.cli import main
from btc_dca_bridge.config import ExecutionConfig
from btc_dca_bridge.operations import OperationsService


class BlockedProductionTests(unittest.TestCase):
    def test_current_blocker_is_external_market_contract(self):
        blockers = active_production_blockers(now=datetime(2026, 10, 7, 12, tzinfo=UTC))
        self.assertEqual(len(blockers), 1)
        blocker = blockers[0]
        self.assertEqual(blocker.blocker_id, QUOTE_LIMIT_BLOCKER_ID)
        self.assertEqual(blocker.category, "MARKET_CONTRACT")
        self.assertTrue(blocker.external_dependency)
        self.assertEqual(blocker.authorization_impact, "BLOCKS_REAL_MONEY")
        self.assertIn("No safe workaround is approved", blocker.evidence)

    def test_capability_snapshot_and_drift_never_approve_sources(self):
        before = current_contract_capabilities(now=datetime(2026, 10, 7, 12, tzinfo=UTC))
        added = dict(before.to_dict(), quote_unit_maximum_supported=True)
        drift = compare_contract_capabilities(before, added)
        self.assertEqual(drift.result, "CAPABILITY_ADDED")
        self.assertEqual(contract_status()["approved_quote_unit_limit_source_count"], 0)
        self.assertFalse(contract_status()["capabilities"]["quote_unit_maximum_supported"])

    def test_drift_detects_removal_and_contract_change(self):
        before = current_contract_capabilities()
        removed = dict(before.to_dict(), market_unit_quote_coin_supported=False)
        self.assertEqual(compare_contract_capabilities(before, removed).result, "CAPABILITY_REMOVED")
        changed = dict(before.to_dict(), symbol="ETHUSDT")
        self.assertEqual(compare_contract_capabilities(before, changed).result, "CONTRACT_CHANGED")
        self.assertEqual(compare_contract_capabilities({"symbol": "BTCUSDT"}, before).result, "AMBIGUOUS_CHANGE")

    def test_cli_reports_blocker_and_contract_without_mutation(self):
        self.assertEqual(main(["production-blockers", "--json"]), 0)
        self.assertEqual(main(["contract-status", "--json"]), 0)

    def test_health_distinguishes_healthy_external_block(self):
        root = Path(tempfile.mkdtemp())
        ledger = root / "ledger.jsonl"
        ledger.write_text("")
        with patch("btc_dca_bridge.operations.load_execution_config", return_value=ExecutionConfig("1.0.0", False, True, True, 500, "Bybit", "spot", "BTCUSDT", "not_implemented")):
            result = OperationsService(data_root=root / "data", ledger_path=ledger).health()
        self.assertEqual(result["status"], "HEALTHY_BLOCKED_EXTERNAL_DEPENDENCY")
        self.assertEqual(result["blockers"][0]["category"], "MARKET_CONTRACT")


if __name__ == "__main__":
    unittest.main()
