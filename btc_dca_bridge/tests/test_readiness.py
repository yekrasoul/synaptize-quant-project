import os
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.config import ExecutionConfig
from btc_dca_bridge.execution import InstrumentRules
from btc_dca_bridge.private_bybit import AccountInfo, ApiCredentialInfo, CredentialClassification, SpotQuoteAvailability, WalletBalance
from btc_dca_bridge.cli import main
from btc_dca_bridge.availability import AvailabilityValidationError, SpotQuoteAvailabilityPolicy, validate_spot_quote_availability
from btc_dca_bridge.readiness import ProductionReadinessService, ServerTimeMeasurement, classify_production_account_mode, measure_server_time, production_connectivity
from btc_dca_bridge.quote_limits import QuoteUnitLimitEvidence, QuoteUnitLimitPolicy


class FakeReadinessClient:
    def __init__(self, *, availability=Decimal("100"), liability=False):
        self.credential = ApiCredentialInfo(CredentialClassification.TRADE_CAPABLE, False, {"Spot": ("SpotTrade",), "Wallet": ("WalletRead",)})
        self.account = AccountInfo(6, "REGULAR_MARGIN", "OFF", "2026-10-07T11:59:00Z")
        self.availability, self.liability = availability, liability
        self.rules = InstrumentRules(Decimal("10"), Decimal("0.00001"), Decimal("0.00001"), Decimal("0.01"), max_market_order_qty=Decimal("100000"), market_order_qty_unit="quoteCoin", market_buy_quote_maximum=Decimal("8000000"))
    def credential_info(self): return self.credential
    def account_info(self): return self.account
    def wallet_balances(self):
        amount = Decimal("1") if self.liability else Decimal("0")
        availability = None if self.availability is None else SpotQuoteAvailability(self.availability, "/v5/account/wallet-balance", "availableBalance", "UNIFIED", True, "2026-10-07T11:59:00Z")
        return (WalletBalance("USDT", Decimal("100"), Decimal("0"), amount, amount, Decimal("100"), availability), WalletBalance("BTC", Decimal("0.1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("6000")))
    def instrument_rules(self): return self.rules
    def spot_quote_availability(self):
        balances = self.wallet_balances()
        return balances[0].available_for_spot_quote_buy
    def quote_unit_limit_evidence(self):
        return QuoteUnitLimitEvidence("BTCUSDT", "spot", "Buy", "Market", "quoteCoin", "USDT", "/test/quote-limit", "maxQuote", "USDT", Decimal("100"), True, "2026-10-07T11:59:30Z", "CONFIRMED")
    def server_time_ms(self): return 1791374340000
    def lookup_order(self, client_order_id): return object()
    def order_realtime_probe(self, client_order_id): return ()
    def order_history_probe(self, client_order_id): return ()
    def executions(self, client_order_id): return ()


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ledger = self.root / "ledger.jsonl"; self.ledger.write_text("")
        self.safe = ExecutionConfig("1.0.0", False, True, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "not_implemented")
        self.test_policy = SpotQuoteAvailabilityPolicy(frozenset({("/v5/account/wallet-balance", "availableBalance")}), frozenset({"UNIFIED"}))
        self.common = dict(repo_probe=lambda: {"commit": "abc123", "dirty": False}, server_time_probe=lambda: ServerTimeMeasurement(0.1, 10), filesystem_probe=lambda: (True, "ok"), lock_probe=lambda: (True, "ok"), client_factory=lambda: FakeReadinessClient(), secret_scan_probe=lambda: {"scanner": "test", "scope": [], "status": "PASS", "finding_count": 0}, availability_policy=self.test_policy, quote_limit_policy=QuoteUnitLimitPolicy(frozenset({("/test/quote-limit", "maxQuote")}), max_age=timedelta(minutes=10)))

    def evaluate(self, **overrides):
        values = dict(self.common); values.update(overrides)
        with patch("btc_dca_bridge.readiness.load_execution_config", return_value=self.safe), patch.dict(os.environ, {"BYBIT_API_KEY": "set", "BYBIT_API_SECRET": "set", "TELEGRAM_BOT_TOKEN": "set", "TELEGRAM_CHAT_ID": "set"}), patch("btc_dca_bridge.readiness.load_notification_config", return_value=object()):
            return ProductionReadinessService(data_root=self.root / "data", ledger_path=self.ledger, now=lambda: datetime(2026, 10, 7, 12, tzinfo=UTC), **values).evaluate()

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
        self.assertEqual({endpoint["status"] for endpoint in result["endpoints"]}, {"PASS"})

    def test_clock_measurement_uses_midpoint_and_round_trip(self):
        ticks = iter((1000.0, 1000.1))
        result = measure_server_time(lambda: 1000040, clock=lambda: next(ticks))
        self.assertAlmostEqual(result.delta_seconds, -0.01, places=6)
        self.assertAlmostEqual(result.round_trip_ms, 100.0, places=6)
        self.assertLessEqual(abs(ServerTimeMeasurement(2.0, 1.0).delta_seconds), 2.0)
        self.assertGreater(abs(ServerTimeMeasurement(2.001, 1.0).delta_seconds), 2.0)

    def test_clock_unavailable_is_a_required_blocker(self):
        result = self.evaluate(server_time_probe=lambda: (_ for _ in ()).throw(TimeoutError("timeout")))
        clock = next(item for item in result["checks"] if item["check_id"] == "CLOCK_SKEW")
        self.assertEqual(clock["status"], "UNAVAILABLE")
        self.assertNotEqual(result["status"], "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION")

    def test_account_mode_accepts_uta_2_and_uta_2_pro_with_old_or_recent_update_timestamp(self):
        now = datetime(2026, 10, 7, 12, tzinfo=UTC)
        uta2_ok, uta2_reason = classify_production_account_mode(AccountInfo(5, "REGULAR_MARGIN", "OFF", "2026-09-01T00:00:00Z"), now=now)
        pro_ok, pro_reason = classify_production_account_mode(AccountInfo(6, "REGULAR_MARGIN", "OFF", "2026-10-07T11:59:00Z"), now=now)
        self.assertTrue(uta2_ok)
        self.assertIn("UTA 2.0 status 5", uta2_reason)
        self.assertTrue(pro_ok)
        self.assertIn("UTA 2.0 Pro status 6", pro_reason)

    def test_account_mode_rejects_unsupported_shapes_and_invalid_or_future_timestamp(self):
        now = datetime(2026, 10, 7, 12, tzinfo=UTC)
        for account in (
            AccountInfo(3, "REGULAR_MARGIN", "OFF", "2026-10-07T11:59:00Z"),
            AccountInfo(4, "REGULAR_MARGIN", "OFF", "2026-10-07T11:59:00Z"),
            AccountInfo(1, "REGULAR_MARGIN", "OFF", "2026-10-07T11:59:00Z"),
            AccountInfo(6, "PORTFOLIO_MARGIN", "OFF", "2026-10-07T11:59:00Z"),
            AccountInfo(6, "REGULAR_MARGIN", "ON", "2026-10-07T11:59:00Z"),
            AccountInfo(6, "REGULAR_MARGIN", "OFF", "2026-10-07T12:00:03Z"),
            AccountInfo(6, "REGULAR_MARGIN", "OFF", "future"),
            AccountInfo(6, "REGULAR_MARGIN", "OFF", None),
        ):
            self.assertFalse(classify_production_account_mode(account, now=now)[0])

    def test_provenance_is_required_for_authoritative_availability(self):
        self.assertEqual(self.evaluate(client_factory=lambda: FakeReadinessClient(availability=None))["status"], "NOT_READY")
        decimal_client = FakeReadinessClient()
        decimal_client.wallet_balances = lambda: (WalletBalance("USDT", Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("100"), Decimal("100")), WalletBalance("BTC", Decimal("0.1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("6000")))
        result = self.evaluate(client_factory=lambda: decimal_client)
        self.assertIn("BYBIT_SPOT_AVAILABLE_BALANCE", {item["check_id"] for item in result["blockers"]})

    def test_production_policy_approves_only_exact_official_source(self):
        result = ProductionReadinessService(data_root=self.root / "data", ledger_path=self.ledger, now=lambda: datetime(2026, 10, 7, 12, tzinfo=UTC), **{key: value for key, value in self.common.items() if key not in {"availability_policy", "quote_limit_policy"}}).evaluate()
        self.assertEqual(next(item for item in result["checks"] if item["check_id"] == "BYBIT_SPOT_AVAILABLE_BALANCE")["status"], "FAIL")
        self.assertEqual(result["quote_unit_limit_evidence"]["conclusion"], "CONFIRMED")
        from btc_dca_bridge.availability import APPROVED_SPOT_QUOTE_AVAILABILITY_SOURCES
        self.assertEqual(APPROVED_SPOT_QUOTE_AVAILABILITY_SOURCES, frozenset({("/v5/order/spot-borrow-check", "spotMaxTradeAmount")}))

    def test_shared_availability_validator_rejects_untrusted_values_and_accepts_only_injected_policy(self):
        now = datetime(2026, 10, 7, 12, tzinfo=UTC)
        policy = SpotQuoteAvailabilityPolicy(frozenset({("/approved", "quoteAvailable")}), frozenset({"UNIFIED"}))
        valid = SpotQuoteAvailability(Decimal("100"), "/approved", "quoteAvailable", "UNIFIED", True, "2026-10-07T11:59:30Z")
        self.assertEqual(validate_spot_quote_availability(valid, now=now, policy=policy), Decimal("100"))
        invalid = (
            Decimal("100"), None,
            SpotQuoteAvailability(Decimal("100"), "/approved", "quoteAvailable", "UNIFIED", False, valid.observed_at_utc),
            SpotQuoteAvailability(Decimal("100"), "/wrong", "quoteAvailable", "UNIFIED", True, valid.observed_at_utc),
            SpotQuoteAvailability(Decimal("100"), "/approved", "availableToWithdraw", "UNIFIED", True, valid.observed_at_utc),
            SpotQuoteAvailability(Decimal("100"), "/approved", "quoteAvailable", "OTHER", True, valid.observed_at_utc),
            SpotQuoteAvailability(Decimal("100"), "/approved", "quoteAvailable", "UNIFIED", True, "not-a-time"),
            SpotQuoteAvailability(Decimal("100"), "/approved", "quoteAvailable", "UNIFIED", True, "2026-10-07T10:00:00Z"),
            SpotQuoteAvailability(Decimal("100"), "/approved", "quoteAvailable", "UNIFIED", True, "2026-10-07T12:00:06Z"),
            SpotQuoteAvailability(Decimal("-1"), "/approved", "quoteAvailable", "UNIFIED", True, valid.observed_at_utc),
            SpotQuoteAvailability(Decimal("NaN"), "/approved", "quoteAvailable", "UNIFIED", True, valid.observed_at_utc),
            SpotQuoteAvailability(Decimal("Infinity"), "/approved", "quoteAvailable", "UNIFIED", True, valid.observed_at_utc),
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(AvailabilityValidationError):
                    validate_spot_quote_availability(value, now=now, policy=policy)

    def test_readiness_requires_at_least_the_v1_minimum_availability(self):
        class OfficialAvailabilityClient(FakeReadinessClient):
            def spot_quote_availability(self):
                return SpotQuoteAvailability(self.amount, "/v5/order/spot-borrow-check", "spotMaxTradeAmount", "UNIFIED", True, "2026-10-07T11:59:30Z")
        for amount, expected in ((Decimal("0"), "FAIL"), (Decimal("9.99"), "FAIL"), (Decimal("10"), "PASS"), (Decimal("100"), "PASS")):
            with self.subTest(amount=amount):
                client = OfficialAvailabilityClient()
                client.amount = amount
                result = self.evaluate(client_factory=lambda client=client: client, availability_policy=SpotQuoteAvailabilityPolicy(frozenset({("/v5/order/spot-borrow-check", "spotMaxTradeAmount")}), frozenset({"UNIFIED"})))
                self.assertEqual(next(item for item in result["checks"] if item["check_id"] == "BYBIT_SPOT_AVAILABLE_BALANCE")["status"], expected)

    def test_instrument_proof_covers_all_v1_amounts_and_requires_quote_upper_bound(self):
        result = self.evaluate()
        instrument = next(item for item in result["checks"] if item["check_id"] == "BYBIT_INSTRUMENT")
        self.assertEqual(instrument["status"], "PASS")
        for amount in ("10", "25", "50", "75", "100"):
            self.assertIn(f"'{amount}': 'PASS'", instrument["evidence"])
        no_upper = FakeReadinessClient()
        no_upper.rules = InstrumentRules(Decimal("10"), Decimal("0.00001"), Decimal("0.00001"), Decimal("0.01"), max_market_order_qty=Decimal("100000"))
        blocked = self.evaluate(client_factory=lambda: no_upper)
        self.assertEqual(next(item for item in blocked["checks"] if item["check_id"] == "BYBIT_INSTRUMENT")["status"], "PASS")

    def test_instrument_minimum_above_v1_floor_fails_closed(self):
        client = FakeReadinessClient()
        client.rules = InstrumentRules(Decimal("11"), Decimal("0.00001"), Decimal("0.00001"), Decimal("0.01"), market_buy_quote_maximum=Decimal("8000000"))
        result = self.evaluate(client_factory=lambda: client)
        instrument = next(item for item in result["checks"] if item["check_id"] == "BYBIT_INSTRUMENT")
        self.assertEqual(instrument["status"], "UNAVAILABLE")

    def test_secret_hygiene_finding_and_unavailable_are_required(self):
        finding = self.evaluate(secret_scan_probe=lambda: {"scanner": "fallback", "scope": ["repo"], "status": "FAIL", "finding_count": 1})
        self.assertEqual(next(item for item in finding["checks"] if item["check_id"] == "SECRET_HYGIENE")["status"], "FAIL")
        unavailable = self.evaluate(secret_scan_probe=lambda: {"scanner": "fallback", "scope": ["repo"], "status": "UNAVAILABLE", "finding_count": 0})
        self.assertEqual(next(item for item in unavailable["checks"] if item["check_id"] == "SECRET_HYGIENE")["status"], "UNAVAILABLE")

    def test_connectivity_unavailable_is_nonzero_and_read_only(self):
        class Unavailable(FakeReadinessClient):
            def executions(self, client_order_id): raise RuntimeError("endpoint unavailable")
        result = production_connectivity(client_factory=Unavailable)
        self.assertEqual(result["status"], "READS_UNAVAILABLE")
        self.assertTrue(any(item["endpoint"] == "execution_list" and item["status"] == "UNAVAILABLE" for item in result["endpoints"]))

    def test_connectivity_cli_maps_unavailable_and_malformed_to_nonzero_codes(self):
        with patch("btc_dca_bridge.cli.production_connectivity", return_value={"status": "READS_UNAVAILABLE"}), redirect_stdout(StringIO()):
            self.assertEqual(main(["production-connectivity", "--json"]), 4)
        with patch("btc_dca_bridge.cli.production_connectivity", return_value={"status": "READS_FAILED"}), redirect_stdout(StringIO()):
            self.assertEqual(main(["production-connectivity", "--json"]), 5)

    def test_cli_readiness_is_machine_readable_and_read_only(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["production-readiness", "--json", "--data-root", str(self.root / "data"), "--ledger", str(self.ledger)])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertIn(payload["status"], {"NOT_READY", "READY_FOR_OPERATOR_PREPARATION"})
        self.assertEqual(payload["real_money_authorization"]["granted"], False)


if __name__ == "__main__": unittest.main()
