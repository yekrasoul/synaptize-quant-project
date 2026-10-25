"""Offline semantic parity evidence for the legacy-to-V2 cutover.

The harness compares safety invariants at the pre-network boundary. It does
not compare private class names, helper ordering, or wire serialization.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from ..execution import OrderIntent as LegacyOrderIntent
from ..live_order import SpotMarketBuyRequest
from ..venues.bybit.mapper import ccxt_order_arguments
from ..core.models import OrderIntent as V2OrderIntent

PARITY = "PARITY"
MISMATCH = "MISMATCH"
INTENTIONAL_V2_REPLACEMENT = "INTENTIONAL_V2_REPLACEMENT"


@dataclass(frozen=True)
class ParityRow:
    semantic: str
    legacy: Any
    v2: Any
    status: str
    evidence: str


def _direct_row(
    semantic: str,
    legacy: Any,
    v2: Any,
    evidence: str,
    *,
    normalize: Callable[[Any], Any] = lambda value: value,
) -> ParityRow:
    status = PARITY if normalize(legacy) == normalize(v2) else MISMATCH
    return ParityRow(semantic, legacy, v2, status, evidence)


def build_cutover_parity_matrix(
    legacy_intent: LegacyOrderIntent, v2_intent: V2OrderIntent
) -> tuple[ParityRow, ...]:
    """Return failure-sensitive invariant parity rows for one decision."""

    legacy_payload = SpotMarketBuyRequest(
        legacy_intent.quote_amount_usdt, legacy_intent.client_order_id
    ).to_payload()
    symbol, side, order_type, amount, _price, params = ccxt_order_arguments(v2_intent)
    v2_payload = {
        "category": "spot",
        "symbol": symbol.replace("/", ""),
        "side": side.title(),
        "orderType": order_type.title(),
        "qty": amount,
        **params,
    }

    legacy_market = (
        legacy_intent.exchange.lower(),
        legacy_intent.market_type.lower(),
        legacy_intent.symbol.upper(),
    )
    v2_market = (
        v2_intent.preferred_venue.lower(),
        v2_intent.market.instrument_type.lower(),
        v2_intent.market.canonical_symbol.replace("/", "").upper(),
    )

    identity_compatible = (
        legacy_intent.decision_id == v2_intent.decision_id
        and bool(legacy_intent.client_order_id)
        and bool(v2_intent.client_order_id)
    )

    return (
        _direct_row(
            "venue and market",
            legacy_market,
            v2_market,
            "both normalize to Bybit Spot BTCUSDT",
        ),
        _direct_row(
            "side",
            legacy_intent.side,
            v2_payload["side"],
            "both are market buys",
            normalize=lambda value: str(value).lower(),
        ),
        _direct_row(
            "order type",
            "Market",
            v2_payload["orderType"],
            "both are market orders",
            normalize=lambda value: str(value).lower(),
        ),
        _direct_row(
            "quote-denominated amount",
            legacy_payload["qty"],
            v2_payload["qty"],
            "both carry the decision amount as quote quantity",
            normalize=lambda value: Decimal(str(value)),
        ),
        _direct_row(
            "marketUnit",
            legacy_payload["marketUnit"],
            v2_payload["marketUnit"],
            "both bind quantity to quoteCoin",
        ),
        _direct_row(
            "leverage/borrowing",
            legacy_payload["isLeverage"],
            v2_payload["isLeverage"],
            "both explicitly disable leverage",
            normalize=int,
        ),
        ParityRow(
            "canonical client identity",
            legacy_intent.client_order_id,
            v2_intent.client_order_id,
            INTENTIONAL_V2_REPLACEMENT if identity_compatible else MISMATCH,
            "identity encoding differs, but both bind a deterministic client identity "
            "to the same canonical decision; executable tests prove duplicate and "
            "reconciliation semantics",
        ),
    )


def parity_statuses(matrix: tuple[ParityRow, ...]) -> set[str]:
    return {row.status for row in matrix}
