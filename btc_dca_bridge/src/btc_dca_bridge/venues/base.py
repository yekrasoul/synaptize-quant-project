"""Ports owned by the execution platform; adapters own exchange translation."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..core.models import AccountIdentity, Fill, OrderIntent


@dataclass(frozen=True)
class VenueOrderAcknowledgement:
    venue_order_id: str
    client_order_id: str


@dataclass(frozen=True)
class VenueReconciliation:
    status: str
    venue_order_id: str | None
    fills: tuple[Fill, ...]
    reason: str | None = None


class VenueAdapter(Protocol):
    name: str

    def account_identity(self) -> AccountIdentity: ...
    def quote_balance(self, quote_asset: str) -> Decimal: ...
    def validate_intent(self, intent: OrderIntent) -> None: ...
    def submit_once(self, intent: OrderIntent) -> VenueOrderAcknowledgement: ...
    def reconcile(self, intent: OrderIntent, venue_order_id: str | None) -> VenueReconciliation: ...
