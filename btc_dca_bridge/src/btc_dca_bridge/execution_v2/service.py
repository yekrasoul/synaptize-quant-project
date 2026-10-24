"""One automated Decision -> Risk -> Submit once -> Reconcile flow."""
from __future__ import annotations

from dataclasses import dataclass

from ..core.models import OrderIntent
from ..core.state import ExecutionState, Order
from ..risk.engine import RiskContext, RiskEngine
from ..venues.base import VenueAdapter
from .oms import InMemoryOms


@dataclass(frozen=True)
class ExecutionResult:
    order: Order
    risk_status: str
    reason_codes: tuple[str, ...] = ()


class ExecutionService:
    def __init__(self, *, oms: InMemoryOms, risk_engine: RiskEngine, venue: VenueAdapter) -> None:
        self.oms = oms
        self.risk_engine = risk_engine
        self.venue = venue

    def execute(self, intent: OrderIntent, context: RiskContext) -> ExecutionResult:
        order = self.oms.create(Order(intent))
        assessment = self.risk_engine.evaluate(intent, context)
        if not assessment.approved:
            return ExecutionResult(self.oms.save(order.transition(ExecutionState.BLOCKED, reason=",".join(assessment.reason_codes))), assessment.status, assessment.reason_codes)
        order = self.oms.save(order.transition(ExecutionState.RISK_APPROVED))
        # This final evaluation is deliberately immediately before the network boundary.
        final = self.risk_engine.evaluate(intent, context)
        if not final.approved:
            return ExecutionResult(self.oms.save(order.transition(ExecutionState.BLOCKED, reason=",".join(final.reason_codes))), final.status, final.reason_codes)
        try:
            self.venue.validate_intent(intent)
            self.oms.claim_submission(intent.client_order_id)
        except Exception as exc:
            return ExecutionResult(self.oms.save(order.transition(ExecutionState.FAILED_PRE_SUBMISSION, reason=str(exc))), "BLOCKED", ("PRE_SUBMISSION_FAILURE",))
        order = self.oms.save(order.transition(ExecutionState.SUBMISSION_STARTED))
        try:
            ack = self.venue.submit_once(intent)
        except TimeoutError as exc:
            return ExecutionResult(self.oms.save(order.transition(ExecutionState.AMBIGUOUS, reason=str(exc))), "AMBIGUOUS", ("RECONCILIATION_REQUIRED",))
        except Exception as exc:
            return ExecutionResult(self.oms.save(order.transition(ExecutionState.REJECTED, reason=str(exc))), "REJECTED", ("VENUE_REJECTED",))
        order = self.oms.save(order.transition(ExecutionState.SUBMITTED, venue_order_id=ack.venue_order_id))
        reconciliation = self.venue.reconcile(intent, ack.venue_order_id)
        order = self.oms.save(order.transition(ExecutionState.RECONCILING))
        state = ExecutionState(reconciliation.status)
        return ExecutionResult(self.oms.save(order.transition(state, venue_order_id=reconciliation.venue_order_id, reason=reconciliation.reason)), "APPROVED")
