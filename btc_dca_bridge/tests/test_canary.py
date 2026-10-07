import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType
from btc_dca_bridge.availability import SpotQuoteAvailabilityPolicy
from btc_dca_bridge.canary import CanaryExecutor, CanaryPreparer, CanaryPreparationError
from btc_dca_bridge.config import ExecutionConfig
from btc_dca_bridge.execution import InstrumentRules
from btc_dca_bridge.private_bybit import AccountInfo, ApiCredentialInfo, CredentialClassification, SpotQuoteAvailability, WalletBalance
from btc_dca_bridge.schemas import validate_artifact
from btc_dca_bridge.cli import main


class FakeReadClient:
    def __init__(self, *, credential=None, account=None, balances=None, rules=None, state="conclusively_absent", error=None):
        self.credential = credential or ApiCredentialInfo(CredentialClassification.TRADE_CAPABLE, False, {"Spot": ("SpotTrade",)})
        self.account = account or AccountInfo(6, "REGULAR_MARGIN", "OFF", "1")
        self.balances = balances or (
            WalletBalance("USDT", Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("100"), SpotQuoteAvailability(Decimal("100"), "/test/availability", "quoteAvailable", "TEST", True, "2026-10-07T11:59:30Z")),
            WalletBalance("BTC", Decimal("0.1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("6000")),
        )
        self.rules = rules or InstrumentRules(Decimal("10"), Decimal("0.00001"), Decimal("0.00001"), Decimal("0.01"))
        self.state = state
        self.error = error
        self.calls = []

    def _call(self, name):
        self.calls.append(name)
        if self.error: raise self.error

    def credential_info(self): self._call("credential_info"); return self.credential
    def account_info(self): self._call("account_info"); return self.account
    def wallet_balances(self): self._call("wallet_balances"); return self.balances
    def instrument_rules(self): self._call("instrument_rules"); return self.rules
    def spot_quote_availability(self):
        self._call("spot_quote_availability")
        return next((balance.available_for_spot_quote_buy for balance in self.balances if balance.coin == "USDT"), None)
    def submission_state(self, client_order_id): self._call(f"submission_state:{client_order_id}"); return self.state


def config():
    return ExecutionConfig("1.0.0", False, True, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "not_implemented")


TEST_AVAILABILITY_POLICY = SpotQuoteAvailabilityPolicy(
    frozenset({("/test/availability", "quoteAvailable")}), frozenset({"TEST"}), max_age=timedelta(days=1)
)


class CanaryPreparationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ledger = self.root / "executions.jsonl"
        self.ledger.write_text("")
        self.run_id = "run_20261007T120000Z_canary01"
        self.now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        self.decision = {
            "schema_version": "1.0.0", "decision_id": "decision_canary", "created_at_utc": "2026-10-07T11:59:00Z",
            "strategy_id": "btc_adaptive_dca_v1", "strategy_version": "1.0.0", "market_snapshot_id": "market_test",
            "drawdown_percent": -10, "fear_greed_index": 20, "base_allocation_usd": 25,
            "sentiment_multiplier": 1, "calculated_allocation_usd": 25, "monthly_spent_before_usd": 0,
            "remaining_budget_before_usd": 500, "final_purchase_usd": 25, "status": "approved",
        }
        self.client = FakeReadClient()

    def prepare(self, *, client=None, ledger=None, data=None, state=None):
        return CanaryPreparer(
            artifact_store=ArtifactStore(data or self.root / "data"),
            client=client or self.client,
            execution_config=config(),
            now=lambda: self.now,
            availability_policy=TEST_AVAILABILITY_POLICY,
        ).prepare(self.decision, run_id=self.run_id, calendar_month="2026-10", ledger_path=ledger or self.ledger)

    def test_happy_preparation_is_ready_and_never_has_post_transport(self):
        result = self.prepare()
        manifest = result.manifest
        self.assertEqual(manifest.canary_status, "READY_FOR_MANUAL_APPROVAL")
        self.assertEqual(manifest.approved_amount_usdt, Decimal("25"))
        self.assertEqual(result.order_payload["qty"], "25")
        self.assertEqual(result.order_payload["marketUnit"], "quoteCoin")
        self.assertEqual(manifest.pre_submission_state, "conclusively_absent")
        self.assertEqual(manifest.live_execution_enabled, False)
        self.assertEqual(manifest.kill_switch, True)
        self.assertEqual(len(self.client.calls), 6)
        self.assertTrue(result.artifact_receipt.path.exists())
        self.assertEqual(self.ledger.read_bytes(), b"")
        self.assertFalse((self.root / "data" / "order_submission_attempts").exists())
        self.assertFalse((self.root / "data" / "order_submission_outcomes").exists())
        validate_artifact("canary_manifest", manifest.to_dict())

    def test_decision_schema_is_validated_before_order_intent(self):
        malformed = dict(self.decision)
        del malformed["market_snapshot_id"]
        with self.assertRaisesRegex(CanaryPreparationError, "Decision schema validation failed"):
            CanaryPreparer(artifact_store=ArtifactStore(self.root / "data-schema"), client=self.client,
                           execution_config=config(), now=lambda: self.now, availability_policy=TEST_AVAILABILITY_POLICY).prepare(
                malformed, run_id=self.run_id, calendar_month="2026-10", ledger_path=self.ledger)

        malformed = dict(self.decision, final_purchase_usd="25")
        with self.assertRaisesRegex(CanaryPreparationError, "Decision schema validation failed"):
            CanaryPreparer(artifact_store=ArtifactStore(self.root / "data-type"), client=self.client,
                           execution_config=config(), now=lambda: self.now, availability_policy=TEST_AVAILABILITY_POLICY).prepare(
                malformed, run_id=self.run_id, calendar_month="2026-10", ledger_path=self.ledger)

    def test_only_explicitly_approved_decision_can_be_ready(self):
        for status in ("monthly_cap_reached", "data_unavailable"):
            with self.subTest(status=status):
                result = CanaryPreparer(
                    artifact_store=ArtifactStore(self.root / f"data-status-{status}"), client=self.client,
                    execution_config=config(), now=lambda: self.now, availability_policy=TEST_AVAILABILITY_POLICY).prepare(
                        dict(self.decision, status=status), run_id=self.run_id,
                        calendar_month="2026-10", ledger_path=self.ledger)
                self.assertEqual(result.manifest.canary_status, "BLOCKED")
                self.assertIn("status must be approved", " ".join(result.manifest.reasons))

    def test_missing_authoritative_availability_blocks_without_resizing(self):
        client = FakeReadClient(balances=(
            WalletBalance("USDT", Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("100")),
            WalletBalance("BTC", Decimal("0.1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("6000")),
        ))
        result = self.prepare(client=client, data=self.root / "data-no-authoritative-availability")
        self.assertEqual(result.manifest.canary_status, "BLOCKED")
        self.assertIn("authoritative USDT availability", " ".join(result.manifest.reasons))
        self.assertEqual(result.manifest.approved_amount_usdt, Decimal("25"))
        self.assertEqual(result.order_payload["qty"], "25")

    def test_malformed_availability_and_account_context_fail_closed(self):
        malformed = FakeReadClient(balances=(
            WalletBalance("USDT", Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("100"), "not-a-number"),
            WalletBalance("BTC", Decimal("0.1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("6000")),
        ))
        result = self.prepare(client=malformed, data=self.root / "data-malformed-availability")
        self.assertEqual(result.manifest.canary_status, "BLOCKED")
        self.assertIn("explicit provenance", " ".join(result.manifest.reasons))

        ambiguous_account = FakeReadClient(
            account=AccountInfo(None, "REGULAR_MARGIN", "OFF", "1"),
            balances=(
                WalletBalance("USDT", Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("100"), Decimal("100")),
                WalletBalance("BTC", Decimal("0.1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("6000")),
            ),
        )
        result = self.prepare(client=ambiguous_account, data=self.root / "data-ambiguous-account")
        self.assertEqual(result.manifest.canary_status, "BLOCKED")
        self.assertIn("account verification is incomplete", " ".join(result.manifest.reasons))

    def test_manifest_is_immutable_and_has_fifteen_minute_expiry(self):
        result = self.prepare()
        self.assertEqual(result.manifest.expires_at_utc, "2026-10-07T12:15:00Z")
        self.assertFalse(result.manifest.is_expired(now_utc=self.now + timedelta(minutes=14)))
        self.assertTrue(result.manifest.is_expired(now_utc=self.now + timedelta(minutes=15)))
        with self.assertRaisesRegex(CanaryPreparationError, "EXPIRED"):
            result.manifest.validate_for_execution(now_utc=self.now + timedelta(minutes=15))
        with self.assertRaisesRegex(CanaryPreparationError, "NOT ENABLED"):
            result.manifest.validate_for_execution(now_utc=self.now)
        with self.assertRaises(Exception): self.prepare(data=self.root / "data")

    def test_payload_fingerprint_is_exact_and_deterministic(self):
        first = self.prepare()
        self.assertEqual(len(first.manifest.order_payload_fingerprint), 64)
        self.assertEqual(first.order_payload["category"], "spot")
        self.assertEqual(first.order_payload["symbol"], "BTCUSDT")
        self.assertEqual(first.order_payload["side"], "Buy")
        self.assertEqual(first.order_payload["orderType"], "Market")
        self.assertEqual(first.order_payload["isLeverage"], 0)
        self.assertEqual(first.order_payload["orderFilter"], "Order")

    def test_cap_exceeded_blocks_using_fresh_ledger(self):
        row = {"schema_version": "1.0.0", "execution_id": "execution_test", "executed_at_utc": "2026-10-01T00:00:00Z", "asset": "BTC", "quote_currency": "USDT", "executed_usd": 490, "reference_price_usdt": 100000, "status": "reconciled", "reconciliation": {"source": "Chat 03 — Portfolio & Budget Tracker", "note": "test"}}
        self.ledger.write_text(json.dumps(row) + "\n")
        result = self.prepare()
        self.assertEqual(result.manifest.canary_status, "BLOCKED")
        self.assertIn("monthly cap", " ".join(result.manifest.reasons))
        self.assertEqual(result.manifest.monthly_spent_usd, Decimal("490"))

    def test_non_trade_credentials_block(self):
        for classification in (CredentialClassification.READ_ONLY, CredentialClassification.INVALID, CredentialClassification.UNSAFE_PERMISSION_SCOPE):
            with self.subTest(classification=classification):
                client = FakeReadClient(credential=ApiCredentialInfo(classification, classification is CredentialClassification.READ_ONLY, {"Spot": ("SpotRead",)}))
                result = self.prepare(client=client, data=self.root / f"data-{classification.value}")
                self.assertEqual(result.manifest.canary_status, "BLOCKED")

    def test_liability_or_insufficient_wallet_blocks_without_reducing_amount(self):
        liabilities = FakeReadClient(balances=(WalletBalance("USDT", Decimal("100"), Decimal("0"), Decimal("1"), Decimal("0"), Decimal("100"), Decimal("100")), WalletBalance("BTC", Decimal("0.1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("6000"))))
        result = self.prepare(client=liabilities, data=self.root / "data-liability")
        self.assertEqual(result.manifest.canary_status, "BLOCKED")
        self.assertIn("liability", " ".join(result.manifest.reasons))
        self.assertEqual(result.manifest.approved_amount_usdt, Decimal("25"))

        insufficient = FakeReadClient(balances=(WalletBalance("USDT", Decimal("5"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("5"), Decimal("5")), WalletBalance("BTC", Decimal("0.1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("6000"))))
        result = self.prepare(client=insufficient, data=self.root / "data-insufficient")
        self.assertIn("authoritative USDT availability", " ".join(result.manifest.reasons))
        self.assertEqual(result.order_payload["qty"], "25")

    def test_only_conclusive_absence_passes_reconciliation(self):
        for state in ("active", "ambiguous", "confirmed", "none"):
            with self.subTest(state=state):
                result = self.prepare(client=FakeReadClient(state=state), data=self.root / f"data-state-{state}")
                self.assertEqual(result.manifest.canary_status, "BLOCKED")
        result = self.prepare(client=FakeReadClient(state="conclusively_absent"), data=self.root / "data-absent")
        self.assertEqual(result.manifest.canary_status, "READY_FOR_MANUAL_APPROVAL")

    def test_prior_submission_artifact_blocks(self):
        data = self.root / "data"
        from btc_dca_bridge.live_order import OrderSubmissionAttempt
        from btc_dca_bridge.artifacts import ArtifactType
        from btc_dca_bridge.execution import client_order_id, make_order_intent
        intent = make_order_intent(self.decision, run_id=self.run_id, created_at_utc=self.decision["created_at_utc"])
        attempt = OrderSubmissionAttempt(self.run_id, "canary-" + "a" * 32, "approval-" + "a" * 32, "b" * 64, intent.decision_id, intent.order_intent_id, intent.client_order_id, Decimal("25"), "a" * 64, "Bybit", "spot", "BTCUSDT", "Buy", "Market", self.decision["created_at_utc"])
        ArtifactStore(data).persist(ArtifactType.ORDER_SUBMISSION_ATTEMPT, attempt, run_id=self.run_id)
        result = self.prepare(data=data)
        self.assertEqual(result.manifest.canary_status, "BLOCKED")
        self.assertIn("RECONCILIATION REQUIRED", result.manifest.reasons)

    def test_executor_is_explicitly_disabled(self):
        with self.assertRaisesRegex(CanaryPreparationError, "NOT ENABLED"):
            CanaryExecutor().execute()

    def test_cli_prepare_is_read_only_and_ends_without_submission(self):
        decision_path = self.root / "decision.json"
        decision_path.write_text(json.dumps(self.decision))
        output = StringIO()
        with patch("btc_dca_bridge.cli.BybitPrivateReadClient.from_environment", return_value=self.client), patch("btc_dca_bridge.cli.PRODUCTION_AVAILABILITY_POLICY", TEST_AVAILABILITY_POLICY), redirect_stdout(output):
            code = main(["canary-prepare", "--decision-json", str(decision_path), "--run-id", self.run_id,
                         "--month", "2026-10", "--ledger", str(self.ledger), "--data-root", str(self.root / "cli-data")])
        self.assertEqual(code, 0)
        self.assertIn("CANARY READY FOR MANUAL APPROVAL", output.getvalue())
        self.assertTrue(output.getvalue().rstrip().endswith("NO ORDER SUBMITTED"))
        self.assertFalse(any(name.startswith("post") for name in self.client.calls))


if __name__ == "__main__": unittest.main()
