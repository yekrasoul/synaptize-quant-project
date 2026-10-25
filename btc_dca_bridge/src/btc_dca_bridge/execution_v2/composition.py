"""Canonical dependency-injected V2 execution composition.

This module is the target production composition root.  It owns no CCXT
construction and performs no network I/O itself: a concrete ``VenueAdapter``
must be supplied by infrastructure.  The checked-in CLI and Production
Shadow deliberately do not supply a writable production runtime while the
activation checklist is blocked.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol

from ..accounting.deployment import confirmed_monthly_spend
from ..accounting.service import AccountingService, LedgerEntry
from ..core.models import Asset, Market, OrderIntent, OrderType, Side
from ..execution_v2.oms import ExecutionRepository
from ..execution_v2.pre_submit import PreSubmitPlan, plan_pre_submit
from ..execution_v2.service import (
    ExecutionResult,
    ExecutionService,
    FreshRiskContextProvider,
)
from ..infrastructure.policy import RuntimeExecutionPolicy
from ..risk.engine import RiskContext, RiskEngine
from ..venues.base import VenueAdapter
from ..venues.health import VenueCircuitBreaker


class AccountContextProvider(Protocol):
    """Fresh authenticated account and market safety context, supplied by infrastructure."""

    def current(self, intent: OrderIntent) -> RiskContext: ...


PolicyProvider = Callable[[], RuntimeExecutionPolicy]
LegacyExecutionReader = Callable[[], tuple[Any, ...]]
V2AccountingReader = Callable[[], tuple[LedgerEntry, ...]]


def order_intent_from_decision(
    decision: Mapping[str, Any], *, run_id: str, created_at_utc: str
) -> OrderIntent:
    """Build one immutable V2 identity from the canonical V1 decision."""

    amount = Decimal(str(decision["final_purchase_usd"]))
    decision_id = str(decision["decision_id"])
    client_order_id = "v2-" + hashlib.sha256(
        f"{decision_id}|{run_id}".encode("utf-8")
    ).hexdigest()[:32]
    return OrderIntent(
        intent_id="intent-v2-" + client_order_id[3:],
        strategy_id=str(decision["strategy_id"]),
        decision_id=decision_id,
        market=Market(Asset("BTC"), Asset("USDT")),
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quote_quantity=amount,
        preferred_venue="bybit",
        client_order_id=client_order_id,
        created_at_utc=created_at_utc,
    )


class CanonicalRiskContextProvider:
    """Merge fresh account data with one policy, spend, and health source."""

    def __init__(
        self,
        account_context: AccountContextProvider,
        *,
        policy: PolicyProvider,
        venue_breaker: VenueCircuitBreaker,
        legacy_reader: LegacyExecutionReader,
        v2_reader: V2AccountingReader,
    ) -> None:
        self.account_context = account_context
        self.policy = policy
        self.venue_breaker = venue_breaker
        self.legacy_reader = legacy_reader
        self.v2_reader = v2_reader

    def current(self, intent: OrderIntent) -> RiskContext:
        configured = self.policy()
        account = self.account_context.current(intent)
        spend = confirmed_monthly_spend(
            legacy=self.legacy_reader(),
            v2_entries=self.v2_reader(),
            calendar_month=account.calendar_month,
        )
        health = self.venue_breaker.current().state
        return replace(
            account,
            monthly_spend=spend,
            monthly_cap=configured.monthly_cap_usd,
            runtime_execution_enabled=configured.execution_enabled,
            kill_switch_active=configured.kill_switch_active,
            allowed_venue=configured.allowed_venue,
            allowed_market=configured.allowed_market,
            expected_account_fingerprint=configured.expected_account_fingerprint,
            venue_health=health,
        )


@dataclass(frozen=True)
class V2ExecutionRuntime:
    """One assembled target runtime; the write boundary remains on ExecutionService."""

    risk_engine: RiskEngine
    context_provider: FreshRiskContextProvider
    venue_breaker: VenueCircuitBreaker
    venue: VenueAdapter
    execution_service: ExecutionService
    accounting: AccountingService

    def intent_from_decision(
        self, decision: Mapping[str, Any], *, run_id: str, created_at_utc: str
    ) -> OrderIntent:
        return order_intent_from_decision(
            decision, run_id=run_id, created_at_utc=created_at_utc
        )

    def plan_shadow(
        self,
        decision: Mapping[str, Any],
        *,
        run_id: str,
        created_at_utc: str,
        context: RiskContext | None = None,
    ) -> dict[str, Any]:
        """Plan through the production-equivalent pre-submit boundary only."""

        amount = Decimal(str(decision["final_purchase_usd"]))
        intent = self.intent_from_decision(
            decision, run_id=run_id, created_at_utc=created_at_utc
        )
        if amount <= 0:
            return {"status": "NO_INTENT", "no_order_executed": True}
        if context is None:
            return {
                "status": "NOT_VERIFIED",
                "reason_codes": ["FRESH_ACCOUNT_AND_VENUE_CONTEXT_UNAVAILABLE"],
                "intent_id": intent.intent_id,
                "client_order_id": intent.client_order_id,
                "no_order_executed": True,
            }
        plan = plan_pre_submit(
            intent=intent,
            context=context,
            risk_engine=self.risk_engine,
            venue=self.venue,
        )
        return {
            "status": plan.risk_status,
            "reason_codes": list(plan.reason_codes),
            "intent_id": intent.intent_id,
            "client_order_id": intent.client_order_id,
            "prepared_submission": dict(plan.prepared_submission or {}),
            "would_submit": plan.approved,
            "no_order_executed": True,
        }

    def execute(
        self, intent: OrderIntent, context: RiskContext
    ) -> ExecutionResult:
        """Execute through the target service; callers must enforce activation first."""

        return self.execution_service.execute(intent, context)


def build_v2_execution_runtime(
    *,
    venue: VenueAdapter,
    oms: ExecutionRepository,
    account_context: AccountContextProvider,
    policy: PolicyProvider,
    venue_breaker: VenueCircuitBreaker,
    legacy_reader: LegacyExecutionReader,
    v2_reader: V2AccountingReader,
    accounting_repository: Any | None = None,
    risk_engine: RiskEngine | None = None,
) -> V2ExecutionRuntime:
    """Build the single target V2 composition through explicit injection.

    Infrastructure supplies the authenticated context, policy, repository,
    accounting store, and venue adapter.  In particular, this function never
    creates credentials or imports CCXT, so constructing it cannot itself
    create a hidden transport or bypass the activation gate.
    """

    engine = risk_engine or RiskEngine()
    context_provider = CanonicalRiskContextProvider(
        account_context,
        policy=policy,
        venue_breaker=venue_breaker,
        legacy_reader=legacy_reader,
        v2_reader=v2_reader,
    )
    accounting = AccountingService(accounting_repository)
    service = ExecutionService(
        oms=oms,
        risk_engine=engine,
        venue=venue,
        risk_context_provider=context_provider,
        venue_breaker=venue_breaker,
        accounting=accounting,
    )
    return V2ExecutionRuntime(
        risk_engine=engine,
        context_provider=context_provider,
        venue_breaker=venue_breaker,
        venue=venue,
        execution_service=service,
        accounting=accounting,
    )
