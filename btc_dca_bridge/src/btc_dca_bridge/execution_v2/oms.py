"""Lightweight OMS: identity, state, and exactly-once submission claim."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.state import Order


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

    def claim_submission(self, client_order_id: str) -> None:
        if client_order_id in self._submission_claims:
            raise ValueError("submission already claimed; reconciliation required")
        self._submission_claims.add(client_order_id)
