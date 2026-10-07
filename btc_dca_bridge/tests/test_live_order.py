import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path

from btc_dca_bridge.artifacts import ArtifactStore
from btc_dca_bridge.config import ExecutionConfig
from btc_dca_bridge.execution import JsonInstrumentMetadataProvider, NoSubmissionEvidence, client_order_id, make_order_intent
from btc_dca_bridge.live_order import AmbiguousSubmissionError, LiveApproval, LiveOrderEngine, LiveOrderSafetyError, SpotMarketBuyRequest
from btc_dca_bridge.market_data.http import HttpResponse
from btc_dca_bridge.private_bybit import ApiCredentialInfo, CredentialClassification
from btc_dca_bridge.cli import main


class AbsentEvidence:
    def decision_state(self, decision_id): return "none"
    def client_order_state(self, client_order_id): return "conclusively_absent"


class ActiveEvidence(AbsentEvidence):
    def client_order_state(self, client_order_id): return "active"


class ConfirmedEvidence(AbsentEvidence):
    def client_order_state(self, client_order_id): return "confirmed"


class AmbiguousEvidence(AbsentEvidence):
    def client_order_state(self, client_order_id): return "ambiguous"


class FreshPostAckReconciler:
    def __init__(self, state): self.state, self.calls = state, []
    def reconcile_after_ack(self, client_order_id):
        self.calls.append(client_order_id)
        return self.state


class FakeSubmitTransport:
    def __init__(self, response=None, error=None): self.response, self.error, self.calls = response, error, []
    def submit_spot_market_buy(self, request):
        self.calls.append(request)
        if self.error: raise self.error
        return self.response


class LiveOrderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ledger = self.root / "executions.jsonl"; self.ledger.write_text("")
        self.run_id = "run_20261007T120000Z_a1b2c3d4e5f6"
        decision = {"strategy_id": "btc_adaptive_dca_v1", "strategy_version": "1.0.0", "decision_id": "decision_live", "final_purchase_usd": 25}
        base = make_order_intent(decision, run_id=self.run_id, created_at_utc="2026-10-07T12:00:00Z")
        self.intent = replace(base, live_execution_enabled=True)
        self.decision = decision
        self.config = ExecutionConfig("1.0.0", True, False, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "implemented_disabled")
        self.provider = JsonInstrumentMetadataProvider({"retCode": 0, "result": {"category": "spot", "list": [{"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading", "lotSizeFilter": {"minOrderAmt": "10", "minOrderQty": "0.00001", "basePrecision": "0.000001", "quotePrecision": "0.01", "qtyStep": "0.00001"}, "priceFilter": {"tickSize": "0.01"}}]}})
        self.credential = ApiCredentialInfo(CredentialClassification.TRADE_CAPABLE, False, {"Spot": ("SpotTrade",)})
        self.approval = LiveApproval(self.intent.decision_id, self.intent.order_intent_id, self.intent.client_order_id, Decimal("25"), "2026-10-07T11:00:00Z", "2026-10-07T13:00:00Z")
        self.engine = LiveOrderEngine(artifact_store=ArtifactStore(self.root / "data"), now=lambda: datetime(2026, 10, 7, 12, 0, tzinfo=UTC))

    def ack(self, link=None):
        return HttpResponse(200, {}, json.dumps({"retCode": 0, "retMsg": "OK", "result": {"orderId": "order-1", "orderLinkId": link or self.intent.client_order_id}}).encode())

    def submit(self, transport=None, evidence=None, approval=None, post_ack_reconciler=None):
        return self.engine.submit(self.intent, self.decision, calendar_month="2026-10", ledger_path=self.ledger, execution_config=self.config, approval=approval or self.approval, credential_info=self.credential, instrument_provider=self.provider, submission_state=evidence or AbsentEvidence(), transport=transport or FakeSubmitTransport(self.ack()), run_id=self.run_id, post_ack_reconciler=post_ack_reconciler)

    def test_request_shape_is_narrow_quote_market_buy(self):
        payload = SpotMarketBuyRequest(Decimal("25"), "dca-" + "a" * 32).to_payload()
        self.assertEqual(payload, {"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": "25", "marketUnit": "quoteCoin", "isLeverage": 0, "orderLinkId": "dca-" + "a" * 32, "orderFilter": "Order"})
        with self.assertRaises(ValueError): SpotMarketBuyRequest(Decimal("25.1"), "dca-x")

    def test_production_config_blocks_and_cli_does_not_submit(self):
        decision = self.root / "decision.json"; decision.write_text(json.dumps(self.decision))
        output = StringIO()
        with redirect_stdout(output): code = main(["live-submit", "--decision-json", str(decision)])
        self.assertEqual(code, 0); self.assertIn("LIVE EXECUTION DISABLED", output.getvalue()); self.assertIn("NO ORDER SUBMITTED", output.getvalue())

    def test_ack_is_not_fill_and_does_not_mutate_ledger(self):
        transport = FakeSubmitTransport(self.ack())
        fresh = FreshPostAckReconciler("ambiguous")
        result = self.submit(transport=transport, post_ack_reconciler=fresh)
        self.assertEqual(result.outcome.state, "acknowledged"); self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result.outcome.reconciliation_state, "ambiguous")
        self.assertTrue(result.outcome.ledger_not_mutated)
        self.assertEqual(fresh.calls, [self.intent.client_order_id])
        self.assertNotIn("no_order_executed", result.outcome.to_dict())
        self.assertEqual(self.ledger.read_text(), "")
        self.assertTrue(list((self.root / "data" / "order_submission_attempts").rglob("*.json")))
        self.assertTrue(list((self.root / "data" / "order_submission_outcomes").rglob("*.json")))

    def test_approval_binding_and_expiry_block_before_post(self):
        bad = replace(self.approval, approved_amount_usdt=Decimal("26"))
        transport = FakeSubmitTransport(self.ack())
        with self.assertRaises(LiveOrderSafetyError): self.submit(transport=transport, approval=bad)
        self.assertFalse(transport.calls)
        expired = replace(self.approval, expires_at_utc="2026-10-07T11:59:00Z")
        with self.assertRaises(LiveOrderSafetyError): self.submit(transport=FakeSubmitTransport(self.ack()), approval=expired)

    def test_missing_approval_and_kill_switch_block_before_post(self):
        transport = FakeSubmitTransport(self.ack())
        missing = self.engine.submit(self.intent, self.decision, calendar_month="2026-10", ledger_path=self.ledger,
                                     execution_config=self.config, approval=None, credential_info=self.credential,
                                     instrument_provider=self.provider, submission_state=AbsentEvidence(),
                                     transport=transport, run_id=self.run_id)
        self.assertEqual(missing.outcome.state, "blocked")
        self.assertIn("EXPLICIT LIVE APPROVAL REQUIRED", missing.outcome.ret_msg)
        self.assertFalse(transport.calls)

        kill_switch_config = replace(self.config, kill_switch=True)
        killed = self.engine.submit(self.intent, self.decision, calendar_month="2026-10", ledger_path=self.ledger,
                                    execution_config=kill_switch_config, approval=self.approval,
                                    credential_info=self.credential, instrument_provider=self.provider,
                                    submission_state=AbsentEvidence(), transport=transport, run_id=self.run_id)
        self.assertEqual(killed.outcome.state, "blocked")
        self.assertIn("KILL SWITCH ACTIVE", killed.outcome.ret_msg)
        self.assertFalse(transport.calls)

    def test_existing_active_or_ambiguous_evidence_blocks_post(self):
        transport = FakeSubmitTransport(self.ack())
        with self.assertRaises(LiveOrderSafetyError): self.submit(transport=transport, evidence=ActiveEvidence())
        self.assertFalse(transport.calls)

        with self.assertRaises(LiveOrderSafetyError): self.submit(transport=transport, evidence=AmbiguousEvidence())
        with self.assertRaises(LiveOrderSafetyError): self.submit(transport=transport, evidence=ConfirmedEvidence())
        self.assertFalse(transport.calls)

    def test_prepared_attempt_forces_reconciliation_before_second_post(self):
        transport = FakeSubmitTransport(self.ack())
        first = self.submit(transport=transport)
        self.assertEqual(first.outcome.state, "acknowledged")
        second_transport = FakeSubmitTransport(self.ack())
        with self.assertRaises(LiveOrderSafetyError): self.submit(transport=second_transport)
        self.assertFalse(second_transport.calls)

    def test_mismatched_ack_is_ambiguous_and_not_success(self):
        result = self.submit(transport=FakeSubmitTransport(self.ack("dca-other")))
        self.assertEqual(result.outcome.state, "ambiguous")

    def test_post_ack_uses_fresh_evidence_not_pre_submit_absence(self):
        for state, expected in (("active", "ambiguous"), ("partial", "ambiguous"), ("confirmed", "confirmed"), ("ambiguous", "ambiguous"), ("conclusively_absent", "ambiguous"), ("none", "ambiguous")):
            with self.subTest(state=state):
                self.engine = LiveOrderEngine(
                    artifact_store=ArtifactStore(self.root / f"data-post-{state}"),
                    now=lambda: datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
                )
                fresh = FreshPostAckReconciler(state)
                result = self.submit(
                    transport=FakeSubmitTransport(self.ack()),
                    evidence=AbsentEvidence(),
                    post_ack_reconciler=fresh,
                )
                self.assertEqual(result.outcome.reconciliation_state, expected)
                self.assertEqual(fresh.calls, [self.intent.client_order_id])

    def test_post_ack_read_failure_is_ambiguous(self):
        class FailingReconciler:
            def reconcile_after_ack(self, client_order_id): raise TimeoutError("read failed")
        result = self.submit(transport=FakeSubmitTransport(self.ack()), post_ack_reconciler=FailingReconciler())
        self.assertEqual(result.outcome.reconciliation_state, "ambiguous")

    def test_exchange_and_malformed_responses_never_look_like_fills(self):
        for label, response, expected in (
            ("rejected", HttpResponse(400, {}, b"{}"), "rejected_by_exchange"),
            ("server", HttpResponse(503, {}, b"{}"), "ambiguous"),
            ("malformed", HttpResponse(200, {}, b"not-json"), "ambiguous"),
        ):
            with self.subTest(label=label):
                self.engine = LiveOrderEngine(
                    artifact_store=ArtifactStore(self.root / f"data-{label}"),
                    now=lambda: datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
                )
                result = self.submit(transport=FakeSubmitTransport(response))
                self.assertEqual(result.outcome.state, expected)

    def test_ambiguous_transport_is_not_retried(self):
        transport = FakeSubmitTransport(error=AmbiguousSubmissionError("uncertain"))
        with self.assertRaises(AmbiguousSubmissionError): self.submit(transport=transport)
        self.assertEqual(len(transport.calls), 1)

    def test_unsafe_credential_blocks_post(self):
        credential = replace(self.credential, classification=CredentialClassification.UNSAFE_PERMISSION_SCOPE)
        with self.assertRaises(LiveOrderSafetyError): self.engine.submit(self.intent, self.decision, calendar_month="2026-10", ledger_path=self.ledger, execution_config=self.config, approval=self.approval, credential_info=credential, instrument_provider=self.provider, submission_state=AbsentEvidence(), transport=FakeSubmitTransport(self.ack()), run_id=self.run_id)

    def test_no_generic_post_or_forbidden_order_operations(self):
        transport = FakeSubmitTransport(self.ack())
        self.assertFalse(hasattr(transport, "post")); self.assertFalse(hasattr(transport, "cancel_order")); self.assertFalse(hasattr(transport, "amend_order"))


if __name__ == "__main__": unittest.main()
