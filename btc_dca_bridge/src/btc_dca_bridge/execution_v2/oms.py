"""OMS port: durable lifecycle identity is claimed before a network write."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..core.state import ExecutionState, Order


UNRESOLVED_STATES = frozenset({ExecutionState.SUBMISSION_STARTED, ExecutionState.SUBMITTED, ExecutionState.RECONCILING, ExecutionState.AMBIGUOUS, ExecutionState.PARTIALLY_FILLED})


class ExecutionRepository(Protocol):
    def create(self, order: Order) -> Order: ...
    def save(self, order: Order) -> Order: ...
    def get(self, client_order_id: str) -> Order | None: ...
    def unresolved(self) -> tuple[Order, ...]: ...
    def claim_submission(self, order: Order) -> Order: ...


@dataclass
class InMemoryOms:
    _orders: dict[str, Order] = field(default_factory=dict)
    _submission_claims: set[str] = field(default_factory=set)

    def create(self, order: Order) -> Order:
        prior = self._orders.get(order.intent.client_order_id)
        if prior is not None:
            raise ValueError("duplicate canonical execution identity")
        self._orders[order.intent.client_order_id] = order
        return order

    def save(self, order: Order) -> Order:
        self._orders[order.intent.client_order_id] = order
        return order

    def get(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    def unresolved(self) -> tuple[Order, ...]:
        return tuple(order for order in self._orders.values() if order.state in UNRESOLVED_STATES)

    def claim_submission(self, order: Order) -> Order:
        if order.intent.client_order_id in self._submission_claims:
            raise ValueError("submission already claimed; reconciliation required")
        self._submission_claims.add(order.intent.client_order_id)
        return self.save(order.transition(ExecutionState.SUBMISSION_STARTED))
