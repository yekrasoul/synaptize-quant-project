"""Read-only V2 production-equivalent preflight for Production Shadow."""
from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any, Mapping

from ..core.models import Asset, Market, OrderIntent, OrderType, Side
from ..risk.engine import RiskContext, RiskEngine
from ..venues.bybit.mapper import ccxt_order_arguments


def plan_v2_shadow(decision: Mapping[str, Any], *, run_id: str, created_at_utc: str, monthly_spend: Decimal) -> dict[str, Any]:
    amount = Decimal(str(decision["final_purchase_usd"]))
    if amount <= 0:
        return {"status": "NO_INTENT", "no_order_executed": True}
    decision_id = str(decision["decision_id"])
    client = "v2-" + hashlib.sha256(f"{decision_id}|{run_id}".encode()).hexdigest()[:32]
    intent = OrderIntent("intent-v2-" + client[3:], str(decision["strategy_id"]), decision_id, Market(Asset("BTC"), Asset("USDT")), Side.BUY, OrderType.MARKET, amount, "bybit", client, created_at_utc)
    # Shadow intentionally has no authenticated account state: it proves the
    # same mapping/risk path and remains blocked before claim or POST.
    risk = RiskEngine().evaluate(intent, RiskContext(created_at_utc[:7], monthly_spend, Decimal("500"), False, True, True, None, "UNCONFIGURED", Decimal("0"), False, False, venue_health="healthy"))
    symbol, side, order_type, quote, _, params = ccxt_order_arguments(intent)
    return {"status": risk.status, "reason_codes": list(risk.reason_codes), "intent_id": intent.intent_id, "client_order_id": client, "ccxt_mapping": {"symbol": symbol, "side": side, "type": order_type, "quote_amount": str(quote), "params": params}, "no_order_executed": True}
