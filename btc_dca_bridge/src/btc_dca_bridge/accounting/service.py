"""Accounting accepts only reconciled execution truth, never acknowledgements."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..core.state import ExecutionState, Order
from ..venues.base import VenueReconciliation
from typing import Protocol


@dataclass(frozen=True)
class LedgerEntry:
    execution_id: str
    quote_decrease: Decimal
    base_increase: Decimal
    fee_quantity: Decimal


class AccountingService:
    def __init__(self, repository: "AccountingRepository | None" = None) -> None:
        self.repository = repository

    def post_confirmed(self, order: Order, reconciliation: VenueReconciliation) -> LedgerEntry:
        if order.state is not ExecutionState.CONFIRMED or reconciliation.status != "CONFIRMED" or not reconciliation.fills:
            raise ValueError("accounting requires reconciled confirmed execution evidence")
        entry = LedgerEntry(order.intent.client_order_id, sum((fill.quote_quantity for fill in reconciliation.fills), Decimal("0")), sum((fill.base_quantity for fill in reconciliation.fills), Decimal("0")), sum((fill.fee_quantity for fill in reconciliation.fills), Decimal("0")))
        if self.repository is not None:
            self.repository.post_once(entry)
        return entry
