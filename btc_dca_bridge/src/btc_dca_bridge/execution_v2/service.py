"""One automated Decision -> Risk -> Submit once -> Reconcile flow."""
from __future__ import annotations

from dataclasses import dataclass, replace

from ..core.models import OrderIntent
from ..core.state import ExecutionState, Order
from ..accounting.service import AccountingService
from ..risk.engine import RiskContext, RiskEngine
from ..venues.base import DeterministicVenueRejection, PreSubmissionFailure, VenueAdapter
from ..venues.health import VenueCircuitBreaker
from ..accounting.deployment import confirmed_monthly_spend
from .oms import ExecutionRepository, SubmissionAlreadyClaimed
from .pre_submit import plan_pre_submit
from typing import Protocol


class FreshRiskContextProvider(Protocol):
    def current(self, intent: OrderIntent) -> RiskContext: ...


class VenueHealthRiskContextProvider:
    """Inject the breaker state at the final fresh-risk read."""
    def __init__(self, source: FreshRiskContextProvider, breaker: VenueCircuitBreaker) -> None:
        self.source, self.breaker = source, breaker

    def current(self, intent: OrderIntent) -> RiskContext:
        return replace(self.source.current(intent), venue_health=self.breaker.current().state)


class UnifiedSpendRiskContextProvider:
    """Infrastructure supplies confirmed history; Risk receives only the total."""
    def __init__(self, source: FreshRiskContextProvider, legacy_reader, v2_reader) -> None:
        self.source, self.legacy_reader, self.v2_reader = source, legacy_reader, v2_reader

    def current(self, intent: OrderIntent) -> RiskContext:
        context = self.source.current(intent)
        return replace(context, monthly_spend=confirmed_monthly_spend(legacy=self.legacy_reader(), v2_entries=self.v2_reader(), calendar_month=context.calendar_month))


@dataclass(frozen=True)
class ExecutionResult:
    order: Order
    risk_status: str
    reason_codes: tuple[str, ...] = ()


class ExecutionService:
    def __init__(self, *, oms: ExecutionRepository, risk_engine: RiskEngine, venue: VenueAdapter, risk_context_provider: FreshRiskContextProvider | None = None, venue_breaker: VenueCircuitBreaker | None = None, accounting: AccountingService | None = None) -> None:
        self.oms = oms
        self.risk_engine = risk_engine
        self.venue = venue
        self.risk_context_provider = risk_context_provider
        self.venue_breaker = venue_breaker
        self.accounting = accounting

    def _context(self, intent: OrderIntent, fallback: RiskContext) -> RiskContext:
        return self.risk_context_provider.current(intent) if self.risk_context_provider else fallback

    def execute(self, intent: OrderIntent, context: RiskContext) -> ExecutionResult:
        try:
            order = self.oms.create(Order(intent))
        except ValueError:
            existing = self.oms.get(intent.client_order_id)
            if existing is None:
                raise
            return ExecutionResult(existing, "DUPLICATE_OR_RECONCILIATION_REQUIRED", ("DUPLICATE_EXECUTION_IDENTITY",))
        assessment = self.risk_engine.evaluate(intent, context)
        if not assessment.approved:
            return ExecutionResult(self.oms.save(order.transition(ExecutionState.BLOCKED, reason=",".join(assessment.reason_codes))), assessment.status, assessment.reason_codes)
        order = self.oms.save(order.transition(ExecutionState.RISK_APPROVED))
        return self._submit_risk_approved(order, context)

    def resume_risk_approved(self, order: Order, context: RiskContext) -> ExecutionResult:
        """Resume one durable pre-claim order; used for recovery, never a new identity."""
        current = self.oms.get(order.intent.client_order_id)
        if current is None or current.state is not ExecutionState.RISK_APPROVED:
            return ExecutionResult(current or order, "DUPLICATE_OR_RECONCILIATION_REQUIRED", ("DUPLICATE_EXECUTION_IDENTITY",))
        return self._submit_risk_approved(current, context)

    def _submit_risk_approved(self, order: Order, context: RiskContext) -> ExecutionResult:
        # Re-read mutable controls immediately before the durable network claim.
        plan = plan_pre_submit(intent=order.intent, context=self._context(order.intent, context), risk_engine=self.risk_engine, venue=self.venue)
        if not plan.approved:
            state = ExecutionState.FAILED_PRE_SUBMISSION if plan.reason_codes and plan.reason_codes[0] == "PRE_SUBMISSION_FAILURE" else ExecutionState.BLOCKED
            return ExecutionResult(self.oms.save(order.transition(state, reason=",".join(plan.reason_codes))), plan.risk_status, plan.reason_codes)
        try:
            order = self.oms.claim_submission(order)
        except SubmissionAlreadyClaimed:
            # The repository is canonical: never save or transition stale state.
            existing = self.oms.get(order.intent.client_order_id)
            if existing is None:
                raise
            return ExecutionResult(existing, "DUPLICATE_OR_RECONCILIATION_REQUIRED", ("SUBMISSION_ALREADY_CLAIMED",))
        try:
            ack = self.venue.submit_once(order.intent)
        except DeterministicVenueRejection as exc:
            return ExecutionResult(self.oms.save(order.transition(ExecutionState.REJECTED, reason=str(exc))), "REJECTED", ("VENUE_REJECTED",))
        except Exception as exc:
            if self.venue_breaker is not None:
                self.venue_breaker.record_failure(type(exc).__name__)
            # No post-boundary exception is assumed to prove non-delivery.
            # DeterministicVenueRejection is the only earlier rejected branch.
            return ExecutionResult(self.oms.save(order.transition(ExecutionState.AMBIGUOUS, reason=str(exc))), "AMBIGUOUS", ("RECONCILIATION_REQUIRED",))
        if self.venue_breaker is not None:
            self.venue_breaker.record_success()
        order = self.oms.save(order.transition(ExecutionState.SUBMITTED, venue_order_id=ack.venue_order_id))
        reconciliation = self.venue.reconcile(order.intent, ack.venue_order_id)
        order = self.oms.save(order.transition(ExecutionState.RECONCILING))
        state = ExecutionState(reconciliation.status)
        final_order = order.transition(state, venue_order_id=reconciliation.venue_order_id, reason=reconciliation.reason)
        if self.accounting is not None and state is ExecutionState.CONFIRMED:
            # Accounting is downstream of reconciliation; if this fails the
            # lifecycle remains RECONCILING and can be recovered safely.
            self.accounting.post_confirmed(final_order, reconciliation)
        return ExecutionResult(self.oms.save(final_order), "APPROVED")

    def recover_unresolved(self) -> tuple[ExecutionResult, ...]:
        """Restart path: reconcile durable identities only; never submit again."""
        recovered: list[ExecutionResult] = []
        for order in self.oms.unresolved():
            if order.state is ExecutionState.SUBMISSION_STARTED:
                order = self.oms.save(order.transition(ExecutionState.AMBIGUOUS, reason="restart after durable claim"))
            if order.state in {ExecutionState.AMBIGUOUS, ExecutionState.PARTIALLY_FILLED, ExecutionState.SUBMITTED}:
                order = self.oms.save(order.transition(ExecutionState.RECONCILING))
            reconciliation = self.venue.reconcile(order.intent, order.venue_order_id)
            target = ExecutionState(reconciliation.status)
            order = self.oms.save(order.transition(target, venue_order_id=reconciliation.venue_order_id, reason=reconciliation.reason))
            recovered.append(ExecutionResult(order, "RECOVERED"))
        return tuple(recovered)
