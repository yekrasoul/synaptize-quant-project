import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType
from btc_dca_bridge.config import ExecutionConfig
from btc_dca_bridge.execution import InstrumentRules, make_order_intent
from btc_dca_bridge.ledger import append_execution_once
from btc_dca_bridge.live_order import (AmbiguousSubmissionError, ConfirmedFill, LiveApproval,
    LiveOrderEngine, LiveOrderSafetyError, ReconciliationEvidence, SpotMarketBuyRequest)
from btc_dca_bridge.market_data.http import HttpResponse
from btc_dca_bridge.private_bybit import AccountInfo, ApiCredentialInfo, CredentialClassification, WalletBalance
from btc_dca_bridge.private_bybit import SpotQuoteAvailability
from btc_dca_bridge.availability import SpotQuoteAvailabilityPolicy
from btc_dca_bridge.quote_limits import QuoteUnitLimitEvidence, QuoteUnitLimitPolicy, unavailable_quote_unit_limit
from btc_dca_bridge.schemas import validate_artifact


class FakeReader:
    def __init__(self, state="conclusively_absent"):
        self.state = state
        self.credential = ApiCredentialInfo(CredentialClassification.TRADE_CAPABLE, False, {"Spot": ("SpotTrade",), "Wallet": ("WalletRead",)})
        self.account = AccountInfo(6, "REGULAR_MARGIN", "OFF", "fresh")
        self.balances = (WalletBalance("USDT", Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("100"), SpotQuoteAvailability(Decimal("100"), "/test/availability", "quoteAvailable", "TEST", True, "2026-10-07T12:00:00Z")), WalletBalance("BTC", Decimal("0.1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("6000")))
        self.rules = InstrumentRules(Decimal("10"), Decimal("0.00001"), Decimal("0.00001"), Decimal("0.01"))
    def credential_info(self): return self.credential
    def account_info(self): return self.account
    def wallet_balances(self): return self.balances
    def instrument_rules(self): return self.rules
    def spot_quote_availability(self): return self.balances[0].available_for_spot_quote_buy
    def quote_unit_limit_evidence(self): return QuoteUnitLimitEvidence("BTCUSDT", "spot", "Buy", "Market", "quoteCoin", "USDT", "/test/quote-limit", "maxQuote", "USDT", Decimal("100"), True, "2026-10-07T12:00:00Z", "CONFIRMED")
    def get_rules(self, exchange, market_type, symbol): return self.rules
    def decision_state(self, decision_id): return "none"
    def client_order_state(self, client_order_id): return self.state
    def submission_state(self, client_order_id): return self.state


class FakeTransport:
    def __init__(self, response=None, error=None): self.response = response; self.error = error; self.calls = []
    def submit_spot_market_buy(self, request):
        self.calls.append(request)
        if self.error: raise self.error
        return self.response


class FakeReconciler:
    def __init__(self, evidence): self.evidence = evidence; self.calls = []
    def reconcile_after_ack(self, client_order_id): self.calls.append(client_order_id); return self.evidence


class LiveOrderHardeningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ledger = self.root / "executions.jsonl"; self.ledger.write_text("")
        self.data = self.root / "data"
        self.run_id = "run_20261007T120000Z_live0001"
        self.now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        self.decision = {"schema_version": "1.0.0", "decision_id": "decision_live", "created_at_utc": "2026-10-07T11:59:00Z", "strategy_id": "btc_adaptive_dca_v1", "strategy_version": "1.0.0", "final_purchase_usd": 25}
        self.intent = replace(make_order_intent(self.decision, run_id=self.run_id, created_at_utc=self.decision["created_at_utc"]), live_execution_enabled=True)
        self.config = ExecutionConfig("1.0.0", True, False, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "implemented")
        payload = SpotMarketBuyRequest(Decimal("25"), self.intent.client_order_id).to_payload()
        self.manifest = {"schema_version": "5.4.0", "canary_id": "canary-" + "a" * 32, "run_id": self.run_id, "strategy_id": "btc_adaptive_dca_v1", "strategy_version": "1.0.0", "decision_id": self.intent.decision_id, "order_intent_id": self.intent.order_intent_id, "client_order_id": self.intent.client_order_id, "exchange": "Bybit", "market_type": "spot", "symbol": "BTCUSDT", "side": "Buy", "order_type": "Market", "approved_amount_usdt": "25", "calendar_month": "2026-10", "monthly_spent_usd": "0", "remaining_budget_usd": "500", "credential_classification": "TRADE_CAPABLE", "account_mode": "REGULAR_MARGIN", "account_unified_margin_status": "6", "account_spot_hedging_status": "OFF", "account_updated_time": "fresh", "wallet_usdt": "100", "wallet_btc": "0.1", "authoritative_available_usdt": "100", "availability_method": "explicit authoritative account-mode-specific Spot quote-buy availability", "liability_detected": False, "instrument_min_order_amt": "10", "instrument_qty_step": "0.00001", "instrument_tick_size": "0.01", "pre_submission_state": "conclusively_absent", "order_payload_fingerprint": hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), "prepared_at_utc": "2026-10-07T11:50:00Z", "expires_at_utc": "2026-10-07T12:05:00Z", "live_execution_enabled": False, "kill_switch": True, "canary_status": "READY_FOR_MANUAL_APPROVAL", "reasons": []}
        self.manifest_sha = hashlib.sha256((json.dumps(self.manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        ArtifactStore(self.data).persist(ArtifactType.CANARY_MANIFEST, self.manifest, run_id=self.run_id)
        self.approval = LiveApproval.for_manifest(self.manifest, self.manifest_sha, now_utc=self.now)
        self.approval_receipt = ArtifactStore(self.data).persist(ArtifactType.LIVE_APPROVAL, self.approval, run_id=self.run_id)
        self.availability_policy = SpotQuoteAvailabilityPolicy(frozenset({("/test/availability", "quoteAvailable")}), frozenset({"TEST"}), max_age=timedelta(days=1))
        self.quote_limit_policy = QuoteUnitLimitPolicy(frozenset({("/test/quote-limit", "maxQuote")}), max_age=timedelta(days=1))
        self.engine = LiveOrderEngine(artifact_store=ArtifactStore(self.data), now=lambda: self.now, availability_policy=self.availability_policy, quote_limit_policy=self.quote_limit_policy)
        self.reader = FakeReader()
        self.ack = HttpResponse(200, {}, json.dumps({"retCode": 0, "retMsg": "OK", "result": {"orderId": "order-1", "orderLinkId": self.intent.client_order_id}}).encode())
        self.reconciler = FakeReconciler(ReconciliationEvidence("ambiguous", "order-1"))

    def submit(self, **kwargs):
        values = dict(calendar_month="2026-10", ledger_path=self.ledger, execution_config=self.config, approval=self.approval, approval_sha256=self.approval_receipt.sha256, manifest=self.manifest, manifest_sha256=self.manifest_sha, read_client=self.reader, transport=FakeTransport(self.ack), run_id=self.run_id, post_ack_reconciler=self.reconciler)
        values.update(kwargs)
        return self.engine.submit(self.intent, self.decision, **values)

    def fill(self, execution_id, quantity, quote, fee="0.01", *, order_id="order-1", fee_asset="USDT"):
        return ConfirmedFill(execution_id, order_id, self.intent.client_order_id, Decimal(quantity), Decimal(quote), Decimal(quote) / Decimal(quantity), "2026-10-07T12:00:01Z", Decimal(fee), fee_asset)

    def reconcile_existing(self, evidence, *, run_id="run_20261007T120100Z_recon000"):
        return self.engine.reconcile_existing(self.intent, ledger_path=self.ledger, approval=self.approval, approval_sha256=self.approval_receipt.sha256, manifest=self.manifest, manifest_sha256=self.manifest_sha, run_id=run_id, post_ack_reconciler=FakeReconciler(evidence))

    def test_missing_manifest_read_client_or_reconciler_blocks_before_post(self):
        for field in ("manifest", "read_client", "post_ack_reconciler"):
            with self.subTest(field=field):
                transport = FakeTransport(self.ack)
                args = {field: None, "transport": transport}
                result = self.submit(**args)
                self.assertEqual(result.outcome.state, "blocked")
                self.assertFalse(transport.calls)
                self.assertFalse(list((self.data / "order_submission_attempts").rglob("*.json")) if (self.data / "order_submission_attempts").exists() else [])

    def test_plain_decimal_availability_blocks_before_post(self):
        self.reader.balances = (WalletBalance("USDT", Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("100"), Decimal("100")), self.reader.balances[1])
        transport = FakeTransport(self.ack)
        with self.assertRaises(LiveOrderSafetyError):
            self.submit(transport=transport)
        self.assertEqual(transport.calls, [])

    def test_missing_quote_unit_limit_blocks_before_attempt_and_post(self):
        self.reader.quote_unit_limit_evidence = lambda: unavailable_quote_unit_limit(observed_at_utc="2026-10-07T12:00:00Z")
        transport = FakeTransport(self.ack)
        with self.assertRaises(LiveOrderSafetyError):
            self.submit(transport=transport)
        self.assertEqual(transport.calls, [])
        attempts = list((self.data / "order_submission_attempts").rglob("*.json")) if (self.data / "order_submission_attempts").exists() else []
        self.assertEqual(attempts, [])

    def test_unpersisted_tampered_or_wrong_hash_approval_blocks(self):
        self.approval = replace(self.approval, approval_id="approval-" + "b" * 32)
        with self.assertRaises(LiveOrderSafetyError): self.submit()
        self.approval = LiveApproval.for_manifest(self.manifest, self.manifest_sha, now_utc=self.now)
        with self.assertRaises(LiveOrderSafetyError): self.submit(approval_sha256="0" * 64)
        wrong = dict(self.manifest, canary_id="canary-" + "b" * 32)
        with self.assertRaises(LiveOrderSafetyError): self.submit(manifest=wrong)

    def test_manifest_uses_injected_clock_and_explicit_expiry(self):
        for prepared, expires in (("2026-10-07T12:01:00Z", "2026-10-07T12:15:00Z"), ("2026-10-07T11:40:00Z", "2026-10-07T11:55:00Z"), ("2026-10-07T11:50:00Z", "2026-10-07T12:10:01Z")):
            with self.subTest(prepared=prepared, expires=expires):
                with self.assertRaises(LiveOrderSafetyError): self.submit(manifest=dict(self.manifest, prepared_at_utc=prepared, expires_at_utc=expires))

    def test_approval_ttl_and_future_dates_fail(self):
        future = replace(self.approval, approved_at_utc="2026-10-07T12:01:00Z", expires_at_utc="2026-10-07T12:06:00Z")
        with self.assertRaises(LiveOrderSafetyError): future.validate(self.intent, now_utc=self.now, manifest=self.manifest, manifest_sha256=self.manifest_sha)
        long = replace(self.approval, expires_at_utc="2026-10-07T12:05:01Z")
        with self.assertRaises(LiveOrderSafetyError): long.validate(self.intent, now_utc=self.now, manifest=self.manifest, manifest_sha256=self.manifest_sha)

    def test_ack_without_fill_does_not_write_ledger_and_reconciler_is_fresh(self):
        result = self.submit()
        self.assertEqual(result.outcome.outcome_category, "reconciliation_required")
        self.assertEqual(self.reconciler.calls, [self.intent.client_order_id])
        self.assertEqual(self.ledger.read_text(), "")

    def test_authoritative_fill_is_strict_and_uses_actual_values(self):
        fill = ConfirmedFill("exec-1", "order-1", self.intent.client_order_id, Decimal("0.0002"), Decimal("24.98"), Decimal("124900"), "2026-10-07T12:00:01Z", Decimal("0.01"), "USDT")
        result = self.submit(post_ack_reconciler=FakeReconciler(ReconciliationEvidence("confirmed", "order-1", (fill,))))
        self.assertEqual(result.outcome.outcome_category, "confirmed_execution")
        row = json.loads(self.ledger.read_text())
        self.assertEqual(row["schema_version"], "1.1.0"); self.assertEqual(row["executed_usd"], 24.98); self.assertEqual(row["btc_quantity"], 0.0002)

    def test_attempt_identity_blocks_approval_reuse(self):
        self.submit()
        with self.assertRaises(LiveOrderSafetyError): self.submit()

    def test_ambiguous_transport_is_never_retried(self):
        transport = FakeTransport(error=AmbiguousSubmissionError("uncertain"))
        result = self.submit(transport=transport)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.reconciler.calls, [self.intent.client_order_id])
        self.assertEqual(result.outcome.outcome_category, "reconciliation_required")
        self.assertEqual(self.ledger.read_text(), "")

    def test_ambiguous_transport_with_confirmed_fill_completes_once(self):
        fill = ConfirmedFill("exec-timeout", "order-1", self.intent.client_order_id, Decimal("0.0002"), Decimal("24.98"), Decimal("124900"), "2026-10-07T12:00:01Z", Decimal("0.01"), "USDT")
        reconciler = FakeReconciler(ReconciliationEvidence("confirmed", "order-1", (fill,), self.intent.client_order_id))
        transport = FakeTransport(error=AmbiguousSubmissionError("socket reset"))
        result = self.submit(transport=transport, post_ack_reconciler=reconciler)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(reconciler.calls, [self.intent.client_order_id])
        self.assertEqual(result.outcome.outcome_category, "confirmed_execution")
        self.assertEqual(json.loads(self.ledger.read_text())["execution_id_bybit"], "exec-timeout")

    def test_uncertain_http_and_ack_shapes_reconcile_without_retry(self):
        responses = [
            HttpResponse(503, {}, b"server error"),
            HttpResponse(200, {}, b"not-json"),
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, json.dumps({"retCode": 0, "result": {}}).encode()),
            HttpResponse(200, {}, json.dumps({"retCode": 0, "result": {"orderId": "order-1", "orderLinkId": "other"}}).encode()),
        ]
        for response in responses:
            with self.subTest(response=response):
                self.setUp()
                transport = FakeTransport(response=response)
                reconciler = FakeReconciler(ReconciliationEvidence("ambiguous", "order-1", (), self.intent.client_order_id))
                result = self.submit(transport=transport, post_ack_reconciler=reconciler)
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(reconciler.calls, [self.intent.client_order_id])
                self.assertEqual(result.outcome.outcome_category, "reconciliation_required")
                self.assertEqual(self.ledger.read_text(), "")

    def test_ack_order_id_must_match_reconciled_order_id(self):
        reconciler = FakeReconciler(ReconciliationEvidence("ambiguous", "different-order", (), self.intent.client_order_id))
        result = self.submit(post_ack_reconciler=reconciler)
        self.assertEqual(reconciler.calls, [self.intent.client_order_id])
        self.assertEqual(result.outcome.outcome_category, "reconciliation_required")
        self.assertEqual(self.ledger.read_text(), "")

    def test_fill_order_and_link_identity_must_match(self):
        for order_id, order_link_id in (("different-order", self.intent.client_order_id), ("order-1", "different-link")):
            with self.subTest(order_id=order_id, order_link_id=order_link_id):
                self.setUp()
                fill = ConfirmedFill("exec-identity", order_id, order_link_id, Decimal("0.0002"), Decimal("24.98"), Decimal("124900"), "2026-10-07T12:00:01Z", Decimal("0.01"), "USDT")
                reconciler = FakeReconciler(ReconciliationEvidence("confirmed", "order-1", (fill,), self.intent.client_order_id))
                result = self.submit(post_ack_reconciler=reconciler)
                self.assertEqual(result.outcome.outcome_category, "reconciliation_required")
                self.assertEqual(self.ledger.read_text(), "")

    def test_duplicate_fill_execution_ids_fail_closed(self):
        fill = ConfirmedFill("exec-duplicate", "order-1", self.intent.client_order_id, Decimal("0.0001"), Decimal("12.49"), Decimal("124900"), "2026-10-07T12:00:01Z", Decimal("0.01"), "USDT")
        reconciler = FakeReconciler(ReconciliationEvidence("confirmed", "order-1", (fill, fill), self.intent.client_order_id))
        result = self.submit(post_ack_reconciler=reconciler)
        self.assertEqual(result.outcome.outcome_category, "reconciliation_required")
        self.assertEqual(self.ledger.read_text(), "")

    def test_active_or_partial_reconciliation_never_topups(self):
        for state in ("active", "partial"):
            with self.subTest(state=state):
                self.setUp()
                reconciler = FakeReconciler(ReconciliationEvidence(state, "order-1", (), self.intent.client_order_id))
                transport = FakeTransport(error=AmbiguousSubmissionError("timeout"))
                result = self.submit(transport=transport, post_ack_reconciler=reconciler)
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(reconciler.calls, [self.intent.client_order_id])
                self.assertEqual(result.outcome.outcome_category, "reconciliation_required")
                self.assertEqual(self.ledger.read_text(), "")

    def test_active_order_persists_empty_reconciliation_evidence(self):
        result = self.submit(post_ack_reconciler=FakeReconciler(ReconciliationEvidence("active", "order-1", (), self.intent.client_order_id)))
        self.assertEqual(result.outcome.reconciliation_state, "ambiguous")
        snapshots = ArtifactStore(self.data).submission_reconciliations(decision_id=self.intent.decision_id, canary_id=self.manifest["canary_id"], client_order_id=self.intent.client_order_id, order_id="order-1")
        self.assertEqual(len(snapshots), 1); self.assertEqual(snapshots[0]["reconciliation_state"], "active"); self.assertEqual(snapshots[0]["fills"], [])
        self.assertEqual(self.ledger.read_text(), "")

    def test_partial_fill_evidence_is_immutable_and_does_not_write_ledger(self):
        fill = self.fill("exec-a", "0.0001", "12.49")
        result = self.submit(post_ack_reconciler=FakeReconciler(ReconciliationEvidence("partial", "order-1", (fill,), self.intent.client_order_id)))
        self.assertEqual(result.outcome.reconciliation_state, "partial")
        snapshots = ArtifactStore(self.data).submission_reconciliations(decision_id=self.intent.decision_id, canary_id=self.manifest["canary_id"], client_order_id=self.intent.client_order_id, order_id="order-1")
        self.assertEqual(len(snapshots), 1); self.assertEqual(snapshots[0]["fills"][0]["exec_id"], "exec-a")
        self.assertEqual(snapshots[0]["fills"][0]["fee_asset"], "USDT"); self.assertEqual(self.ledger.read_text(), "")

    def test_multiple_partial_fills_are_preserved_without_ledger_write(self):
        fills = (self.fill("exec-a", "0.0001", "12.49"), self.fill("exec-b", "0.0001", "12.50"))
        self.submit(post_ack_reconciler=FakeReconciler(ReconciliationEvidence("partial", "order-1", fills, self.intent.client_order_id)))
        snapshots = ArtifactStore(self.data).submission_reconciliations(decision_id=self.intent.decision_id, canary_id=self.manifest["canary_id"], client_order_id=self.intent.client_order_id, order_id="order-1")
        self.assertEqual([item["exec_id"] for item in snapshots[0]["fills"]], ["exec-a", "exec-b"])
        self.assertEqual(self.ledger.read_text(), "")

    def test_partial_missing_fee_asset_fails_closed(self):
        fill = self.fill("exec-a", "0.0001", "12.49", fee_asset="")
        result = self.submit(post_ack_reconciler=FakeReconciler(ReconciliationEvidence("partial", "order-1", (fill,), self.intent.client_order_id)))
        self.assertEqual(result.outcome.outcome_category, "reconciliation_required")
        self.assertFalse((self.data / "submission_reconciliations").exists())
        self.assertEqual(self.ledger.read_text(), "")

    def test_partial_to_filled_recovery_aggregates_unique_fills_once(self):
        first = self.fill("exec-a", "0.0001", "12.49")
        self.submit(post_ack_reconciler=FakeReconciler(ReconciliationEvidence("partial", "order-1", (first,), self.intent.client_order_id)))
        second = self.fill("exec-b", "0.0001", "12.50")
        result = self.reconcile_existing(ReconciliationEvidence("confirmed", "order-1", (first, second), self.intent.client_order_id))
        self.assertEqual(result.outcome.outcome_category, "confirmed_execution")
        row = json.loads(self.ledger.read_text())
        self.assertEqual(row["executed_usd"], 24.99); self.assertEqual(row["btc_quantity"], 0.0002)
        self.assertEqual(row["execution_id_bybit"], "exec-a,exec-b")
        replay = self.reconcile_existing(ReconciliationEvidence("confirmed", "order-1", (first, second), self.intent.client_order_id), run_id="run_20261007T120200Z_recon001")
        self.assertEqual(replay.outcome.outcome_category, "confirmed_execution")
        self.assertEqual(len(self.ledger.read_text().splitlines()), 1)

    def test_conflicting_prior_fill_evidence_blocks_final_ledger_append(self):
        for quantity, fee, order_id in (("0.0002", "0.01", "order-1"), ("0.0001", "0.02", "order-1"), ("0.0001", "0.01", "other-order")):
            with self.subTest(quantity=quantity, fee=fee, order_id=order_id):
                self.setUp()
                first = self.fill("exec-a", "0.0001", "12.49")
                self.submit(post_ack_reconciler=FakeReconciler(ReconciliationEvidence("partial", "order-1", (first,), self.intent.client_order_id)))
                changed = self.fill("exec-a", quantity, "12.49", fee=fee, order_id=order_id)
                result = self.reconcile_existing(ReconciliationEvidence("confirmed", "order-1", (changed,), self.intent.client_order_id), run_id="run_20261007T120200Z_recon001")
                self.assertEqual(result.outcome.outcome_category, "reconciliation_required")
                self.assertEqual(self.ledger.read_text(), "")

    def test_partial_to_partial_preserves_evidence_without_post_or_topup(self):
        first = self.fill("exec-a", "0.0001", "12.49")
        transport = FakeTransport(self.ack)
        self.submit(transport=transport, post_ack_reconciler=FakeReconciler(ReconciliationEvidence("partial", "order-1", (first,), self.intent.client_order_id)))
        second = self.fill("exec-b", "0.0001", "12.50")
        result = self.reconcile_existing(ReconciliationEvidence("partial", "order-1", (first, second), self.intent.client_order_id))
        self.assertEqual(len(transport.calls), 1); self.assertEqual(result.outcome.outcome_category, "reconciliation_required")
        self.assertEqual(self.ledger.read_text(), "")

    def test_missing_authoritative_fee_asset_fails_closed(self):
        fill = ConfirmedFill("exec-no-fee-asset", "order-1", self.intent.client_order_id, Decimal("0.0002"), Decimal("24.98"), Decimal("124900"), "2026-10-07T12:00:01Z", Decimal("0.01"), "")
        reconciler = FakeReconciler(ReconciliationEvidence("confirmed", "order-1", (fill,), self.intent.client_order_id))
        result = self.submit(post_ack_reconciler=reconciler)
        self.assertEqual(result.outcome.outcome_category, "reconciliation_required")
        self.assertEqual(self.ledger.read_text(), "")

    def test_exactly_once_ledger_append_and_conflict(self):
        payload = {"schema_version": "1.1.0", "execution_id": "execution_x", "executed_at_utc": "2026-10-07T12:00:00Z", "asset": "BTC", "quote_currency": "USDT", "executed_usd": 25, "reference_price_usdt": 100000, "btc_quantity": .00025, "status": "reconciled", "reconciliation": {"source": "Bybit private order/fill reconciliation", "note": "test"}, "decision_id": "d", "canary_id": "canary-" + "a" * 32, "approval_id": "approval-" + "a" * 32, "order_id": "o", "order_link_id": "dca-" + "a" * 32, "execution_id_bybit": "x", "fee": 0, "fee_asset": "USDT"}
        self.assertTrue(append_execution_once(self.ledger, payload)); self.assertFalse(append_execution_once(self.ledger, payload))
        with self.assertRaises(Exception): append_execution_once(self.ledger, dict(payload, executed_usd=26))

    def test_execution_schema_keeps_legacy_and_live_sources_strict(self):
        legacy = {"schema_version": "1.0.0", "execution_id": "execution_legacy", "executed_at_utc": "2026-10-07T12:00:00Z", "asset": "BTC", "quote_currency": "USDT", "executed_usd": 25, "reference_price_usdt": 100000, "status": "reconciled", "reconciliation": {"source": "Chat 03 — Portfolio & Budget Tracker", "note": "legacy"}}
        validate_artifact("execution", legacy)
        with self.assertRaises(Exception): validate_artifact("execution", dict(legacy, reconciliation={"source": "arbitrary", "note": "bad"}))
        with self.assertRaises(Exception): validate_artifact("execution", {"schema_version": "1.1.0", "execution_id": "execution_incomplete", "executed_at_utc": "2026-10-07T12:00:00Z", "asset": "BTC", "quote_currency": "USDT", "executed_usd": 25, "reference_price_usdt": 100000, "btc_quantity": .00025, "status": "reconciled", "reconciliation": {"source": "Bybit private order/fill reconciliation", "note": "missing live identity"}})

    def test_prepared_claim_is_recoverable_after_interruption(self):
        payload = {"schema_version": "1.1.0", "execution_id": "execution_recover", "executed_at_utc": "2026-10-07T12:00:00Z", "asset": "BTC", "quote_currency": "USDT", "executed_usd": 25, "reference_price_usdt": 100000, "btc_quantity": .00025, "status": "reconciled", "reconciliation": {"source": "Bybit private order/fill reconciliation", "note": "recovery"}, "decision_id": "d", "canary_id": "canary-" + "a" * 32, "approval_id": "approval-" + "a" * 32, "order_id": "o", "order_link_id": "dca-" + "a" * 32, "execution_id_bybit": "x", "fee": 0, "fee_asset": "USDT"}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        claim = self.ledger.parent / f".{self.ledger.name}.{payload['execution_id']}.claim"
        claim.write_text(json.dumps({"execution_id": payload["execution_id"], "payload_sha256": hashlib.sha256(canonical.encode()).hexdigest(), "status": "prepared"}))
        self.assertTrue(append_execution_once(self.ledger, payload))


if __name__ == "__main__": unittest.main()
