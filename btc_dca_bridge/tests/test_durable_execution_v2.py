from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from btc_dca_bridge.accounting.service import AccountingService
from btc_dca_bridge.core.models import AccountIdentity, Asset, Fill, Market, OrderIntent, OrderType, Side
from btc_dca_bridge.core.state import ExecutionState, Order
from btc_dca_bridge.execution_v2.service import ExecutionService
from btc_dca_bridge.infrastructure.accounting_repository import JsonAccountingRepository
from btc_dca_bridge.infrastructure.execution_repository import JsonExecutionRepository
from btc_dca_bridge.risk.engine import RiskContext, RiskEngine
from btc_dca_bridge.venues.base import AmbiguousSubmissionError, DeterministicVenueRejection, VenueOrderAcknowledgement, VenueReconciliation


def intent() -> OrderIntent:
    return OrderIntent("intent-durable", "adaptive_dca_v1", "decision-durable", Market(Asset("BTC"), Asset("USDT")), Side.BUY, OrderType.MARKET, Decimal("25"), "bybit", "dca-durable-1", "2026-10-24T00:00:00Z")


def context(**changes) -> RiskContext:
    values = dict(calendar_month="2026-10", monthly_spend=Decimal("0"), monthly_cap=Decimal("500"), runtime_execution_enabled=True, kill_switch_active=False, market_data_fresh=True, account_identity=AccountIdentity("bybit", "account"), expected_account_fingerprint="account", quote_balance=Decimal("100"), unresolved_execution_exists=False, duplicate_execution_exists=False, venue_health="healthy")
    values.update(changes); return RiskContext(**values)


class Venue:
    name = "bybit"
    def __init__(self, outcome="ack", reconcile="CONFIRMED"): self.outcome, self.reconcile_state, self.posts, self.lookups = outcome, reconcile, 0, []
    def account_identity(self): return AccountIdentity("bybit", "account")
    def quote_balance(self, _): return Decimal("100")
    def validate_intent(self, _): return None
    def submit_once(self, order):
        self.posts += 1
        if self.outcome == "timeout": raise TimeoutError("post timeout")
        if self.outcome == "reset": raise ConnectionResetError("reset after post")
        if self.outcome == "reject": raise DeterministicVenueRejection("insufficient venue balance")
        if self.outcome == "outage": raise AmbiguousSubmissionError("gateway unavailable")
        return VenueOrderAcknowledgement("venue-1", order.client_order_id)
    def reconcile(self, order, venue_order_id):
        self.lookups.append((order.client_order_id, venue_order_id))
        if self.reconcile_state == "AMBIGUOUS": return VenueReconciliation("AMBIGUOUS", None, (), "not found by client identity")
        fill = Fill("fill-1", venue_order_id or "resolved-by-client", Decimal("0.001"), Decimal("25"), Decimal("0"), Asset("USDT"), "2026-10-24T00:00:01Z")
        return VenueReconciliation(self.reconcile_state, venue_order_id or "resolved-by-client", (fill,) if self.reconcile_state == "CONFIRMED" else ())


class Provider:
    def __init__(self, value): self.value, self.calls = value, 0
    def current(self, _): self.calls += 1; return self.value


class DurableExecutionV2Tests(unittest.TestCase):
    def service(self, root, venue, provider=None):
        return ExecutionService(oms=JsonExecutionRepository(Path(root) / "executions"), risk_engine=RiskEngine(), venue=venue, risk_context_provider=provider)

    def test_durable_claim_precedes_post_and_restart_prevents_second_post(self):
        with TemporaryDirectory() as root:
            venue = Venue("timeout")
            result = self.service(root, venue).execute(intent(), context())
            self.assertEqual((result.order.state, venue.posts), (ExecutionState.AMBIGUOUS, 1))
            restarted = self.service(root, venue)
            self.assertEqual(restarted.oms.get(intent().client_order_id).state, ExecutionState.AMBIGUOUS)
            with self.assertRaises(ValueError): restarted.execute(intent(), context())
            self.assertEqual(venue.posts, 1)

    def test_restart_after_claim_recovers_by_client_identity_without_post(self):
        with TemporaryDirectory() as root:
            repo = JsonExecutionRepository(Path(root) / "executions")
            claimed = repo.claim_submission(repo.save(repo.create(Order(intent())).transition(ExecutionState.RISK_APPROVED)))
            self.assertEqual(claimed.state, ExecutionState.SUBMISSION_STARTED)
            venue = Venue(reconcile="CONFIRMED")
            recovered = ExecutionService(oms=repo, risk_engine=RiskEngine(), venue=venue).recover_unresolved()
            self.assertEqual((recovered[0].order.state, venue.posts, venue.lookups), (ExecutionState.CONFIRMED, 0, [(intent().client_order_id, None)]))

    def test_only_deterministic_rejection_is_rejected(self):
        with TemporaryDirectory() as root:
            self.assertEqual(self.service(root, Venue("reject")).execute(intent(), context()).order.state, ExecutionState.REJECTED)
        for outcome in ("timeout", "reset", "outage"):
            with TemporaryDirectory() as root:
                self.assertEqual(self.service(root, Venue(outcome)).execute(intent(), context()).order.state, ExecutionState.AMBIGUOUS)

    def test_fresh_provider_can_block_after_preflight_before_durable_claim(self):
        with TemporaryDirectory() as root:
            provider = Provider(context(kill_switch_active=True))
            result = self.service(root, Venue(), provider).execute(intent(), context())
            self.assertEqual((result.order.state, provider.calls), (ExecutionState.BLOCKED, 1))

    def test_accounting_repository_is_idempotent_and_reconciliation_gated(self):
        with TemporaryDirectory() as root:
            order = Order(intent()).transition(ExecutionState.RISK_APPROVED).transition(ExecutionState.SUBMISSION_STARTED).transition(ExecutionState.SUBMITTED, venue_order_id="venue-1").transition(ExecutionState.RECONCILING).transition(ExecutionState.CONFIRMED)
            fill = Fill("fill-1", "venue-1", Decimal(".001"), Decimal("25"), Decimal("0"), Asset("USDT"), "2026-10-24T00:00:01Z")
            service = AccountingService(JsonAccountingRepository(Path(root) / "accounting"))
            self.assertEqual(service.post_confirmed(order, VenueReconciliation("CONFIRMED", "venue-1", (fill,))).execution_id, intent().client_order_id)
            self.assertEqual(len(list((Path(root) / "accounting").glob("*.json"))), 1)
            service.post_confirmed(order, VenueReconciliation("CONFIRMED", "venue-1", (fill,)))
            self.assertEqual(len(list((Path(root) / "accounting").glob("*.json"))), 1)


if __name__ == "__main__": unittest.main()
