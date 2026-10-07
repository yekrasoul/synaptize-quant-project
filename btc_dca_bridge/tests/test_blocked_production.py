import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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
from btc_dca_bridge.quote_limits import QuoteUnitLimitEvidence, QuoteUnitLimitPolicy


class BlockedProductionTests(unittest.TestCase):
    NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
    TEST_SOURCE = ("/test/quote-limit", "maxQuote")

    def evidence(self, **overrides):
        values = {
            "symbol": "BTCUSDT", "market_type": "spot", "side": "Buy",
            "order_type": "Market", "market_unit": "quoteCoin",
            "quote_currency": "USDT", "source_endpoint": self.TEST_SOURCE[0],
            "source_field": self.TEST_SOURCE[1], "unit": "USDT",
            "maximum_quote_usdt": Decimal("100"), "authoritative": True,
            "observed_at_utc": self.NOW.isoformat().replace("+00:00", "Z"),
            "conclusion": "CONFIRMED",
        }
        values.update(overrides)
        return QuoteUnitLimitEvidence(**values)

    @property
    def test_policy(self):
        return QuoteUnitLimitPolicy(frozenset({self.TEST_SOURCE}))

    def test_current_blocker_is_external_market_contract(self):
        blockers = active_production_blockers(now=self.NOW)
        self.assertEqual(len(blockers), 1)
        blocker = blockers[0]
        self.assertEqual(blocker.blocker_id, QUOTE_LIMIT_BLOCKER_ID)
        self.assertEqual(blocker.category, "MARKET_CONTRACT")
        self.assertTrue(blocker.external_dependency)
        self.assertEqual(blocker.authorization_impact, "BLOCKS_REAL_MONEY")
        self.assertIn("No safe workaround is approved", blocker.evidence)

    def test_blocker_requires_policy_and_validated_evidence(self):
        valid = self.evidence()
        scenarios = (
            (QuoteUnitLimitPolicy(frozenset()), valid),
            (self.test_policy, None),
            (self.test_policy, self.evidence(conclusion="NOT_EXPOSED", authoritative=False, maximum_quote_usdt=None)),
            (self.test_policy, self.evidence(source_endpoint="/wrong")),
            (self.test_policy, self.evidence(observed_at_utc=(self.NOW - timedelta(minutes=11)).isoformat())),
            (self.test_policy, self.evidence(authoritative=False)),
        )
        for policy, evidence in scenarios:
            with self.subTest(policy=policy, evidence=evidence):
                self.assertEqual(len(active_production_blockers(now=self.NOW, quote_limit_policy=policy, quote_limit_evidence=evidence)), 1)
        self.assertEqual(active_production_blockers(now=self.NOW, quote_limit_policy=self.test_policy, quote_limit_evidence=valid), ())

    def test_production_policy_remains_blocked_even_with_valid_test_evidence(self):
        self.assertEqual(contract_status()["approved_quote_unit_limit_source_count"], 0)
        self.assertTrue(active_production_blockers(now=self.NOW, quote_limit_evidence=self.evidence()))

    def test_capability_snapshot_uses_same_validated_evidence(self):
        confirmed = current_contract_capabilities(now=self.NOW, quote_limit_policy=self.test_policy, quote_limit_evidence=self.evidence())
        self.assertTrue(confirmed.quote_unit_maximum_supported)
        self.assertEqual(confirmed.quote_unit_maximum_source, "/test/quote-limit:maxQuote")
        production = current_contract_capabilities(now=self.NOW)
        self.assertFalse(production.quote_unit_maximum_supported)
        self.assertIsNone(production.quote_unit_maximum_source)
        status = contract_status(now=self.NOW, quote_limit_policy=self.test_policy, quote_limit_evidence=self.evidence())
        self.assertEqual(status["production_quote_limit_conclusion"], "QUOTE_UNIT_MAX_CONFIRMED")
        self.assertEqual(status["capabilities"]["quote_unit_maximum_source"], "/test/quote-limit:maxQuote")

    def test_capability_snapshot_and_drift_never_approve_sources(self):
        before = current_contract_capabilities(now=self.NOW)
        added = dict(before.to_dict(), quote_unit_maximum_supported=True)
        drift = compare_contract_capabilities(before, added)
        self.assertEqual(drift.result, "CAPABILITY_ADDED")
        self.assertEqual(contract_status()["approved_quote_unit_limit_source_count"], 0)
        self.assertFalse(contract_status()["capabilities"]["quote_unit_maximum_supported"])
        # Capability drift is informational; it never changes production policy/evidence.
        self.assertTrue(active_production_blockers(now=self.NOW))

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

    def _health(self, *, ledger_text="", blocker_evaluator=None):
        root = Path(tempfile.mkdtemp())
        ledger = root / "ledger.jsonl"
        ledger.write_text(ledger_text)
        safe = ExecutionConfig("1.0.0", False, True, True, 500, "Bybit", "spot", "BTCUSDT", "not_implemented")
        with patch("btc_dca_bridge.operations.load_execution_config", return_value=safe):
            return OperationsService(data_root=root / "data", ledger_path=ledger, blocker_evaluator=blocker_evaluator).health()

    def test_health_healthy_without_external_blocker(self):
        self.assertEqual(self._health(blocker_evaluator=lambda **_: ()) ["status"], "HEALTHY")

    def test_health_unresolved_reconciliation_takes_precedence_over_external_blocker(self):
        root = Path(tempfile.mkdtemp())
        ledger = root / "ledger.jsonl"
        ledger.write_text("")
        fake_snapshot = SimpleNamespace(reconciliation_required=True, to_dict=lambda: {"reconciliation_required": True})
        blocker = active_production_blockers(now=self.NOW)
        with patch("btc_dca_bridge.operations.load_execution_config", return_value=ExecutionConfig("1.0.0", False, True, True, 500, "Bybit", "spot", "BTCUSDT", "not_implemented")), patch.object(OperationsService, "snapshot", return_value=fake_snapshot):
            result = OperationsService(data_root=root / "data", ledger_path=ledger, blocker_evaluator=lambda **_: blocker).health()
        self.assertEqual(result["status"], "HEALTHY_WITH_UNRESOLVED_RECONCILIATION")
        self.assertEqual(result["external_dependency_state"], "PRODUCTION_BLOCKED_EXTERNAL_CONTRACT")
        self.assertEqual(len(result["external_blockers"]), 1)

    def test_health_corruption_precedes_external_blocker(self):
        result = self._health(ledger_text="not-json\n")
        self.assertEqual(result["status"], "CORRUPT")

    def test_health_unsafe_config_precedes_external_blocker(self):
        root = Path(tempfile.mkdtemp())
        ledger = root / "ledger.jsonl"
        ledger.write_text("")
        unsafe = ExecutionConfig("1.0.0", True, False, True, 500, "Bybit", "spot", "BTCUSDT", "implemented")
        with patch("btc_dca_bridge.operations.load_execution_config", return_value=unsafe):
            result = OperationsService(data_root=root / "data", ledger_path=ledger).health()
        self.assertEqual(result["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
