"""The one canonical execution state machine.

An ambiguous post-network result is terminal for automatic submission: only
read-only reconciliation may establish a later confirmed outcome.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from .models import OrderIntent


class ExecutionState(StrEnum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMISSION_STARTED = "SUBMISSION_STARTED"
    SUBMITTED = "SUBMITTED"
    RECONCILING = "RECONCILING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    FAILED_PRE_SUBMISSION = "FAILED_PRE_SUBMISSION"
    AMBIGUOUS = "AMBIGUOUS"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"


_TRANSITIONS = {
    ExecutionState.CREATED: {ExecutionState.RISK_APPROVED, ExecutionState.BLOCKED},
    ExecutionState.RISK_APPROVED: {ExecutionState.SUBMISSION_STARTED, ExecutionState.BLOCKED},
    ExecutionState.SUBMISSION_STARTED: {ExecutionState.SUBMITTED, ExecutionState.FAILED_PRE_SUBMISSION, ExecutionState.AMBIGUOUS, ExecutionState.REJECTED},
    ExecutionState.SUBMITTED: {ExecutionState.RECONCILING, ExecutionState.AMBIGUOUS},
    ExecutionState.RECONCILING: {ExecutionState.CONFIRMED, ExecutionState.PARTIALLY_FILLED, ExecutionState.CANCELLED, ExecutionState.REJECTED, ExecutionState.AMBIGUOUS},
    ExecutionState.AMBIGUOUS: {ExecutionState.RECONCILING},
    ExecutionState.PARTIALLY_FILLED: {ExecutionState.RECONCILING},
}


@dataclass(frozen=True)
class Order:
    intent: OrderIntent
    state: ExecutionState = ExecutionState.CREATED
    venue_order_id: str | None = None
    reason: str | None = None

    def transition(self, state: ExecutionState, *, venue_order_id: str | None = None, reason: str | None = None) -> "Order":
        if state not in _TRANSITIONS.get(self.state, set()):
            raise ValueError(f"invalid execution transition: {self.state} -> {state}")
        if self.state is ExecutionState.AMBIGUOUS and state is ExecutionState.SUBMISSION_STARTED:
            raise ValueError("ambiguous execution can never be resubmitted")
        return replace(self, state=state, venue_order_id=venue_order_id or self.venue_order_id, reason=reason)
