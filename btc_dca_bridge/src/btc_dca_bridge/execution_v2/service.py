"""One automated Decision -> Risk -> Submit once -> Reconcile flow."""
from __future__ import annotations

from dataclasses import dataclass

from ..core.models import OrderIntent
from ..core.state import ExecutionState, Order
from ..risk.engine import RiskContext, RiskEngine
from ..venues.base import DeterministicVenueRejection, PreSubmissionFailure, VenueAdapter
from .oms import ExecutionRepository, SubmissionAlreadyClaimed
from .pre_submit import plan_pre_submit
from typing import Protocol


class FreshRiskContextProvider(Protocol):
    def current(self, intent: OrderIntent) -> RiskContext: ...


@dataclass(frozen=True)
class ExecutionResult:
    order: Order
    risk_status: str
    reason_codes: tuple[str, ...] = ()


class ExecutionService:
    def __init__(self, *, oms: ExecutionRepository, risk_engine: RiskEngine, venue: VenueAdapter, risk_context_provider: FreshRiskContextProvider | None = None) -> None:
        self.oms = oms
        self.risk_engine = risk_engine
        self.venue = venue
        self.risk_context_provider = risk_context_provider

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
            # No post-boundary exception is assumed to prove non-delivery.
            # DeterministicVenueRejection is the only earlier rejected branch.
            return ExecutionResult(self.oms.save(order.transition(ExecutionState.AMBIGUOUS, reason=str(exc))), "AMBIGUOUS", ("RECONCILIATION_REQUIRED",))
        order = self.oms.save(order.transition(ExecutionState.SUBMITTED, venue_order_id=ack.venue_order_id))
        reconciliation = self.venue.reconcile(order.intent, ack.venue_order_id)
        order = self.oms.save(order.transition(ExecutionState.RECONCILING))
        state = ExecutionState(reconciliation.status)
        return ExecutionResult(self.oms.save(order.transition(state, venue_order_id=reconciliation.venue_order_id, reason=reconciliation.reason)), "APPROVED")

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
