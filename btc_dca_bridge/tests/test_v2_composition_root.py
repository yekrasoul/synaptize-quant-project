from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from contextlib import contextmanager

from btc_dca_bridge.accounting.service import AccountingService
from btc_dca_bridge.core.models import AccountIdentity, Asset, Fill, Market, OrderIntent, OrderType, Side
from btc_dca_bridge.execution_v2.composition import (
    CanonicalRiskContextProvider,
    build_v2_execution_runtime,
)
from btc_dca_bridge.execution_v2.oms import InMemoryOms
from btc_dca_bridge.execution_v2.service import ExecutionService
from btc_dca_bridge.infrastructure.accounting_repository import JsonAccountingRepository
from btc_dca_bridge.infrastructure.policy import RuntimeExecutionPolicy
from btc_dca_bridge.risk.engine import RiskContext, RiskEngine
from btc_dca_bridge.venues.base import VenueOrderAcknowledgement, VenueReconciliation
from btc_dca_bridge.venues.health import VenueCircuitBreaker


def intent() -> OrderIntent:
    return OrderIntent(
        "intent-composition",
        "adaptive_dca_v1",
        "decision-composition",
        Market(Asset("BTC"), Asset("USDT")),
        Side.BUY,
        OrderType.MARKET,
        Decimal("25"),
        "bybit",
        "v2-composition-client",
        "2026-10-25T00:00:00Z",
    )


def account_context() -> RiskContext:
    return RiskContext(
        "2026-10",
        Decimal("0"),
        Decimal("500"),
        True,
        False,
        True,
        AccountIdentity("bybit", "account-1"),
        "account-1",
        Decimal("100"),
        False,
        False,
        venue_health="healthy",
    )


class AccountProvider:
    def __init__(self) -> None:
        self.calls = 0

    def current(self, _intent: OrderIntent) -> RiskContext:
        self.calls += 1
        return account_context()


class Venue:
    name = "bybit"

    def __init__(self) -> None:
        self.posts = 0
        self.validations = 0

    def account_identity(self):
        return AccountIdentity("bybit", "account-1")

    def quote_balance(self, _quote: str) -> Decimal:
        return Decimal("100")

    def validate_intent(self, _intent: OrderIntent) -> None:
        self.validations += 1

    def prepare_submission(self, value: OrderIntent):
        return {"symbol": value.market.canonical_symbol, "client_order_id": value.client_order_id}

    def submit_once(self, value: OrderIntent) -> VenueOrderAcknowledgement:
        self.posts += 1
        return VenueOrderAcknowledgement("venue-1", value.client_order_id)

    def reconcile(self, _intent: OrderIntent, venue_order_id: str | None):
        fill = Fill("fill-1", venue_order_id or "venue-1", Decimal("0.001"), Decimal("25"), Decimal("0"), Asset("USDT"), "2026-10-25T00:00:01Z")
        return VenueReconciliation("CONFIRMED", venue_order_id or "venue-1", (fill,))


class V2CompositionRootTests(unittest.TestCase):
    @contextmanager
    def build(self, *, execution_enabled: bool = True, policy_provider=None):
        account = AccountProvider()
        venue = Venue()
        breaker = VenueCircuitBreaker()
        breaker.record_success()
        policy = policy_provider or (lambda: RuntimeExecutionPolicy(execution_enabled, not execution_enabled, Decimal("500"), "bybit", "BTC/USDT:spot", "account-1"))
        with TemporaryDirectory() as root:
            accounting = JsonAccountingRepository(Path(root) / "accounting")
            runtime = build_v2_execution_runtime(
                venue=venue,
                oms=InMemoryOms(),
                account_context=account,
                policy=policy,
                venue_breaker=breaker,
                legacy_reader=lambda: (),
                v2_reader=accounting.read_confirmed,
                accounting_repository=accounting,
            )
            yield runtime, account, venue, accounting

    def test_root_wires_risk_oms_ems_venue_and_confirmed_accounting(self):
        with self.build() as (runtime, account, venue, accounting):
            self.assertIsInstance(runtime.context_provider, CanonicalRiskContextProvider)
            self.assertIsInstance(runtime.execution_service, ExecutionService)
            self.assertIsInstance(runtime.accounting, AccountingService)
            result = runtime.execute(intent(), runtime.context_provider.current(intent()))
            self.assertEqual((result.order.state.value, venue.posts, account.calls), ("CONFIRMED", 1, 2))
            self.assertEqual(len(accounting.read_confirmed()), 1)

    def test_one_fresh_kill_switch_blocks_before_claim_and_post(self):
        with self.build(execution_enabled=False) as (runtime, _account, venue, _accounting):
            context = runtime.context_provider.current(intent())
            result = runtime.execute(intent(), context)
            self.assertEqual(result.risk_status, "BLOCKED")
            self.assertIn("KILL_SWITCH_ACTIVE", result.reason_codes)
            self.assertEqual((venue.posts, len(runtime.execution_service.oms.unresolved())), (0, 0))

    def test_final_policy_reread_blocks_after_initial_context(self):
        states = iter((True, False))
        with self.build(policy_provider=lambda: RuntimeExecutionPolicy(next(states), False, Decimal("500"), "bybit", "BTC/USDT:spot", "account-1")) as (runtime, _account, venue, _accounting):
            initial = runtime.context_provider.current(intent())
            result = runtime.execute(intent(), initial)
            self.assertEqual(result.risk_status, "BLOCKED")
            self.assertIn("EXECUTION_POLICY_DISABLED", result.reason_codes)
            self.assertEqual((venue.posts, len(runtime.execution_service.oms.unresolved())), (0, 0))

    def test_shadow_plan_uses_same_pre_submit_boundary_without_claim_or_post(self):
        with self.build() as (runtime, _account, venue, accounting):
            result = runtime.plan_shadow(
                {"decision_id": "decision-shadow", "strategy_id": "adaptive_dca_v1", "final_purchase_usd": 25},
                run_id="run_20261025T000000Z_shadow",
                created_at_utc="2026-10-25T00:00:00Z",
                context=runtime.context_provider.current(intent()),
            )
            self.assertTrue(result["would_submit"])
            self.assertTrue(result["no_order_executed"])
            self.assertEqual((venue.validations, venue.posts, accounting.read_confirmed()), (1, 0, ()))

    def test_root_requires_explicit_dependencies_and_has_no_ccxt_import(self):
        source = Path(__file__).parents[1] / "src" / "btc_dca_bridge" / "execution_v2" / "composition.py"
        self.assertNotIn("import ccxt", source.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
