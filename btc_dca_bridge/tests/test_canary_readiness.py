import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.availability import SpotQuoteAvailabilityPolicy
from btc_dca_bridge.cli import main
from btc_dca_bridge.config import ExecutionConfig
from btc_dca_bridge.execution import InstrumentRules
from btc_dca_bridge.private_bybit import AccountInfo, ApiCredentialInfo, CredentialClassification, SpotQuoteAvailability, WalletBalance
from btc_dca_bridge.readiness import ProductionReadinessService, ServerTimeMeasurement
from btc_dca_bridge.canary_readiness import BybitCanaryReadinessAudit


class _Client:
    def credential_info(self):
        return ApiCredentialInfo(CredentialClassification.TRADE_CAPABLE, False, {"Spot": ("SpotTrade",), "Wallet": ("WalletRead",)})

    def account_info(self):
        return AccountInfo(6, "REGULAR_MARGIN", "OFF", "1791633600000")

    def wallet_balances(self):
        return (
            WalletBalance("USDT", Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("100"), None),
            WalletBalance("BTC", Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None),
        )

    def spot_quote_availability(self):
        return SpotQuoteAvailability(Decimal("100"), "/v5/order/spot-borrow-check", "spotMaxTradeAmount", "UNIFIED", True, "2026-10-10T16:59:00Z")

    def instrument_rules(self):
        return InstrumentRules(Decimal("10"), Decimal("0.00001"), Decimal("0.00001"), Decimal("0.01"), max_market_order_qty=Decimal("100000"), market_order_qty_unit="quoteCoin")

    def quote_unit_limit_evidence(self):
        from btc_dca_bridge.quote_limits import unavailable_quote_unit_limit
        return unavailable_quote_unit_limit(observed_at_utc="2026-10-10T16:59:00Z")

    def server_time_ms(self):
        return 1791648000000

    def order_realtime_probe(self, client_order_id): return ()
    def order_history_probe(self, client_order_id): return ()
    def executions(self, client_order_id): return ()


class CanaryReadinessTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ledger = self.root / "ledger.jsonl"
        self.ledger.write_text("")
        self.now = datetime(2026, 10, 10, 17, tzinfo=UTC)
        self.policy = SpotQuoteAvailabilityPolicy(frozenset({("/v5/order/spot-borrow-check", "spotMaxTradeAmount")}), frozenset({"UNIFIED"}))
        self.safe = ExecutionConfig("1.0.0", False, True, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "not_implemented")

    def _audit(self, client_factory=None, local=None, **overrides):
        local = local or {"checks": {"production_shadow": {"state": "HEALTHY", "reason": "scheduled shadow healthy"}}}
        kwargs = {
            "client_factory": client_factory or (lambda: _Client()),
            "repo_probe": lambda: {"commit": "abc", "dirty": False},
            "server_time_probe": lambda: ServerTimeMeasurement(0.1, 10),
            "filesystem_probe": lambda: (True, "ok"),
            "lock_probe": lambda: (True, "ok"),
            "secret_scan_probe": lambda: {"status": "PASS", "scanner": "test", "finding_count": 0, "scope": []},
            "availability_policy": self.policy,
            "local_audit_factory": lambda **unused: local,
        }
        kwargs.update(overrides)
        with patch("btc_dca_bridge.readiness.load_execution_config", return_value=self.safe), patch("btc_dca_bridge.readiness.load_notification_config", return_value=object()), patch.dict(os.environ, {"BYBIT_API_KEY": "key", "BYBIT_API_SECRET": "secret"}, clear=False):
            return BybitCanaryReadinessAudit(data_root=self.root / "data", ledger_path=self.ledger, now=lambda: self.now, **kwargs).evaluate()

    def test_all_read_only_checks_can_prove_readiness_without_post(self):
        result = self._audit()
        self.assertEqual(result["readiness"], "CANARY_READY_READ_ONLY")
        self.assertTrue(result["authenticated_reads_only"])
        self.assertFalse(result["order_submission_attempted"])
        self.assertEqual(result["execution"], "BLOCKED")
        self.assertEqual(result["message"], "NO ORDER EXECUTED")

    def test_missing_environment_credentials_is_explicit_blocker(self):
        with patch.dict(os.environ, {}, clear=True):
            result = BybitCanaryReadinessAudit(
                data_root=self.root / "data", ledger_path=self.ledger, now=lambda: self.now,
                client_factory=lambda: (_ for _ in ()).throw(RuntimeError("credentials unavailable")),
                local_audit_factory=lambda **unused: {"checks": {"production_shadow": {"state": "HEALTHY", "reason": "healthy"}}},
                repo_probe=lambda: {"commit": "abc", "dirty": False},
                server_time_probe=lambda: ServerTimeMeasurement(0.1, 10), filesystem_probe=lambda: (True, "ok"),
                lock_probe=lambda: (True, "ok"), secret_scan_probe=lambda: {"status": "PASS"},
            ).evaluate()
        self.assertEqual(result["readiness"], "CANARY_NOT_READY")
        self.assertIn("SECRET_BYBIT_API_KEY", {item["check_id"] for item in result["blockers"]})
        self.assertIn("SECRET_BYBIT_API_SECRET", {item["check_id"] for item in result["blockers"]})

    def test_unhealthy_shadow_blocks_even_when_bybit_reads_pass(self):
        result = self._audit(local={"checks": {"production_shadow": {"state": "UNKNOWN", "reason": "no local shadow"}}})
        self.assertEqual(result["readiness"], "CANARY_NOT_READY")
        self.assertIn("PRODUCTION_SHADOW", {item["check_id"] for item in result["blockers"]})

    def test_insufficient_usdt_wallet_is_a_blocker(self):
        client = _Client()
        client.wallet_balances = lambda: (WalletBalance("USDT", Decimal("5"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("5"), None), WalletBalance("BTC", Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None))
        result = self._audit(client_factory=lambda: client)
        self.assertEqual(result["readiness"], "CANARY_NOT_READY")
        self.assertIn("BYBIT_USDT_WALLET_BALANCE", {item["check_id"] for item in result["blockers"]})

    def test_prior_attempt_blocks_and_does_not_write_ledger(self):
        attempts = self.root / "data" / "order_submission_attempts" / "2026" / "10" / "10"
        attempts.mkdir(parents=True)
        (attempts / "prior.json").write_text(json.dumps({"client_order_id": "dca-" + "a" * 32}))
        before = self.ledger.read_bytes()
        result = self._audit()
        self.assertEqual(result["readiness"], "CANARY_NOT_READY")
        self.assertIn("SUBMISSION_ATTEMPTS", {item["check_id"] for item in result["blockers"]})
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_legacy_live_execution_attempt_also_blocks(self):
        attempts = self.root / "data" / "live_execution_attempts" / "2026" / "10" / "10"
        attempts.mkdir(parents=True)
        (attempts / "prior.json").write_text(json.dumps({"attempt_id": "prior"}))
        result = self._audit()
        self.assertEqual(result["readiness"], "CANARY_NOT_READY")
        self.assertIn("SUBMISSION_ATTEMPTS", {item["check_id"] for item in result["blockers"]})

    def test_cli_parser_accepts_command_and_returns_blocked_json(self):
        output = StringIO()
        with patch("btc_dca_bridge.cli.bybit_canary_readiness", return_value={"readiness": "CANARY_NOT_READY", "blockers": [], "message": "NO ORDER EXECUTED"}), redirect_stdout(output):
            code = main(["bybit-canary-readiness", "--data-root", str(self.root / "data"), "--ledger", str(self.ledger)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["readiness"], "CANARY_NOT_READY")


if __name__ == "__main__":
    unittest.main()
