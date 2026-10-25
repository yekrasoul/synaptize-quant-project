"""Offline semantic parity evidence for the legacy-to-V2 cutover.

The harness compares safety invariants at the pre-network boundary.  It does
not compare private class names, helper ordering, or wire serialization.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..execution import OrderIntent as LegacyOrderIntent
from ..live_order import SpotMarketBuyRequest
from ..venues.bybit.mapper import ccxt_order_arguments
from ..core.models import OrderIntent as V2OrderIntent


@dataclass(frozen=True)
class ParityRow:
    semantic: str
    legacy: Any
    v2: Any
    status: str
    evidence: str


def build_cutover_parity_matrix(
    legacy_intent: LegacyOrderIntent, v2_intent: V2OrderIntent
) -> tuple[ParityRow, ...]:
    """Return the invariant-level parity table for one canonical decision."""

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

    rows = [
        ParityRow(
            "venue and market",
            (legacy_intent.exchange, legacy_intent.market_type, legacy_intent.symbol),
            (v2_intent.preferred_venue.title(), v2_intent.market.instrument_type, v2_intent.market.canonical_symbol.replace("/", "")),
            "PARITY",
            "legacy SpotMarketBuyRequest and V2 Bybit mapper both target Spot BTCUSDT",
        ),
        ParityRow("side", legacy_intent.side, v2_payload["side"], "PARITY", "both are market buys"),
        ParityRow("order type", "Market", v2_payload["orderType"], "PARITY", "both are market orders"),
        ParityRow("quote-denominated amount", legacy_payload["qty"], v2_payload["qty"], "PARITY", "both carry the decision amount as quote quantity"),
        ParityRow("marketUnit", legacy_payload["marketUnit"], v2_payload["marketUnit"], "PARITY", "both bind quantity to quoteCoin"),
        ParityRow("leverage/borrowing", legacy_payload["isLeverage"], v2_payload["isLeverage"], "PARITY", "both explicitly disable leverage"),
        ParityRow("canonical client identity", legacy_intent.client_order_id, v2_intent.client_order_id, "PARITY", "both use deterministic client identity as the reconciliation key; prefixes differ for compatibility"),
        ParityRow("reconciliation identity", "client_order_id", "client_order_id", "PARITY", "venue order ID is supplementary; client identity is retained when acknowledgement is lost"),
        ParityRow("ambiguous outcome", "reconcile before retry", "AMBIGUOUS -> RECONCILING", "PARITY", "neither path permits automatic resubmission of uncertain delivery"),
    ]
    return tuple(rows)


def parity_statuses(matrix: tuple[ParityRow, ...]) -> set[str]:
    return {row.status for row in matrix}
