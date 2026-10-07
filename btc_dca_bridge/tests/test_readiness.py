import os
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.config import ExecutionConfig
from btc_dca_bridge.execution import InstrumentRules
from btc_dca_bridge.private_bybit import AccountInfo, ApiCredentialInfo, CredentialClassification, WalletBalance
from btc_dca_bridge.cli import main
from btc_dca_bridge.readiness import ProductionReadinessService, production_connectivity


class FakeReadinessClient:
    def __init__(self, *, availability=Decimal("100"), liability=False):
        self.credential = ApiCredentialInfo(CredentialClassification.TRADE_CAPABLE, False, {"Spot": ("SpotTrade",), "Wallet": ("WalletRead",)})
        self.account = AccountInfo(6, "REGULAR_MARGIN", "OFF", "fresh")
        self.availability, self.liability = availability, liability
        self.rules = InstrumentRules(Decimal("10"), Decimal("0.00001"), Decimal("0.00001"), Decimal("0.01"), max_market_order_qty=Decimal("100000"))
    def credential_info(self): return self.credential
    def account_info(self): return self.account
    def wallet_balances(self):
        amount = Decimal("1") if self.liability else Decimal("0")
        return (WalletBalance("USDT", Decimal("100"), Decimal("0"), amount, amount, Decimal("100"), self.availability), WalletBalance("BTC", Decimal("0.1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("6000")))
    def instrument_rules(self): return self.rules


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ledger = self.root / "ledger.jsonl"; self.ledger.write_text("")
        self.safe = ExecutionConfig("1.0.0", False, True, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "not_implemented")
        self.common = dict(repo_probe=lambda: {"commit": "abc123", "dirty": False}, server_time_probe=lambda: 0.1, filesystem_probe=lambda: (True, "ok"), lock_probe=lambda: (True, "ok"), client_factory=lambda: FakeReadinessClient())

    def evaluate(self, **overrides):
        values = dict(self.common); values.update(overrides)
        with patch("btc_dca_bridge.readiness.load_execution_config", return_value=self.safe), patch.dict(os.environ, {"BYBIT_API_KEY": "set", "BYBIT_API_SECRET": "set", "TELEGRAM_BOT_TOKEN": "set", "TELEGRAM_CHAT_ID": "set"}), patch("btc_dca_bridge.readiness.load_notification_config", return_value=object()):
            return ProductionReadinessService(data_root=self.root / "data", ledger_path=self.ledger, now=lambda: datetime(2026, 10, 7, tzinfo=UTC), **values).evaluate()

    def test_all_mocked_technical_checks_pass_but_authorization_stays_false(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION")
        self.assertEqual(result["real_money_authorization"], {"granted": False, "required": True, "status": "NOT_AUTHORIZED"})

    def test_missing_authoritative_availability_is_not_ready(self):
        result = self.evaluate(client_factory=lambda: FakeReadinessClient(availability=None))
        self.assertEqual(result["status"], "NOT_READY")
        self.assertIn("BYBIT_SPOT_AVAILABLE_BALANCE", {item["check_id"] for item in result["blockers"]})

    def test_multiple_safety_blockers_are_reported(self):
        result = self.evaluate(client_factory=lambda: FakeReadinessClient(liability=True), server_time_probe=lambda: 5.0, repo_probe=lambda: {"commit": "abc", "dirty": True})
        self.assertEqual(result["status"], "NOT_READY")
        ids = {item["check_id"] for item in result["blockers"]}
        self.assertTrue({"REPOSITORY_CLEAN", "BYBIT_LIABILITIES", "CLOCK_SKEW"}.issubset(ids))

    def test_unsafe_config_is_blocking(self):
        unsafe = ExecutionConfig("1.0.0", True, False, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "implemented")
        with patch("btc_dca_bridge.readiness.load_execution_config", return_value=unsafe):
            result = ProductionReadinessService(data_root=self.root / "data", ledger_path=self.ledger, **self.common).evaluate()
        self.assertEqual(next(item for item in result["checks"] if item["check_id"] == "CONFIG_DEFAULTS")["status"], "FAIL")

    def test_connectivity_uses_only_injected_read_client(self):
        result = production_connectivity(client_factory=lambda: FakeReadinessClient())
        self.assertEqual(result["status"], "READS_OK")
        self.assertTrue(result["read_only"])
        self.assertEqual(result["message"], "NO ORDER SUBMITTED")

    def test_cli_readiness_is_machine_readable_and_read_only(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["production-readiness", "--json", "--data-root", str(self.root / "data"), "--ledger", str(self.ledger)])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertIn(payload["status"], {"NOT_READY", "READY_FOR_OPERATOR_PREPARATION"})
        self.assertEqual(payload["real_money_authorization"]["granted"], False)


if __name__ == "__main__": unittest.main()
