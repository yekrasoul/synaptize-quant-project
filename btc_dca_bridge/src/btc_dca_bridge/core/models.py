"""Canonical, exchange-independent execution values.

These objects intentionally have no CCXT or venue wire-format fields.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass(frozen=True)
class Asset:
    code: str

    def __post_init__(self) -> None:
        if not self.code or self.code != self.code.upper() or not self.code.isalnum():
            raise ValueError("asset code must be upper-case alphanumeric")


@dataclass(frozen=True)
class Market:
    base: Asset
    quote: Asset
    instrument_type: str = "spot"

    def __post_init__(self) -> None:
        if self.instrument_type != "spot":
            raise ValueError("only spot instruments are currently supported")

    @property
    def canonical_symbol(self) -> str:
        return f"{self.base.code}/{self.quote.code}"


@dataclass(frozen=True)
class AccountIdentity:
    venue: str
    fingerprint: str
    account_id: str | None = None
    subaccount_id: str | None = None
    environment: str | None = None
    credential_scope_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.venue or not self.fingerprint:
            raise ValueError("account identity requires venue and fingerprint")


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    strategy_id: str
    decision_id: str
    market: Market
    side: Side
    order_type: OrderType
    quote_quantity: Decimal
    preferred_venue: str
    client_order_id: str
    created_at_utc: str

    def __post_init__(self) -> None:
        if not self.intent_id or not self.strategy_id or not self.decision_id or not self.client_order_id:
            raise ValueError("order intent identity is incomplete")
        if not self.preferred_venue:
            raise ValueError("order intent requires a venue")
        if not self.quote_quantity.is_finite() or self.quote_quantity <= 0:
            raise ValueError("quote quantity must be positive and finite")


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    base_quantity: Decimal
    quote_quantity: Decimal
    fee_quantity: Decimal
    fee_asset: Asset
    occurred_at_utc: str

    def __post_init__(self) -> None:
        if not self.fill_id or not self.order_id:
            raise ValueError("fill identity is incomplete")
        if self.base_quantity <= 0 or self.quote_quantity <= 0 or self.fee_quantity < 0:
            raise ValueError("fill amounts are invalid")
