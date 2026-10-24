"""Read-only V2 production-equivalent preflight for Production Shadow."""
from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any, Mapping

from ..core.models import Asset, Market, OrderIntent, OrderType, Side
from ..risk.engine import RiskContext, RiskEngine
from ..venues.base import VenueAdapter
from .pre_submit import plan_pre_submit


def plan_v2_shadow(decision: Mapping[str, Any], *, run_id: str, created_at_utc: str, monthly_spend: Decimal, context: RiskContext | None = None, venue: VenueAdapter | None = None) -> dict[str, Any]:
    amount = Decimal(str(decision["final_purchase_usd"]))
    if amount <= 0:
        return {"status": "NO_INTENT", "no_order_executed": True}
    decision_id = str(decision["decision_id"])
    client = "v2-" + hashlib.sha256(f"{decision_id}|{run_id}".encode()).hexdigest()[:32]
    intent = OrderIntent("intent-v2-" + client[3:], str(decision["strategy_id"]), decision_id, Market(Asset("BTC"), Asset("USDT")), Side.BUY, OrderType.MARKET, amount, "bybit", client, created_at_utc)
    # Do not fabricate authenticated account, venue-health, or runtime policy
    # evidence. A production shadow without those safe inputs reports its
    # pre-submit parity as unverified rather than pretending to be blocked.
    if context is None or venue is None:
        return {"status": "NOT_VERIFIED", "reason_codes": ["FRESH_ACCOUNT_AND_VENUE_CONTEXT_UNAVAILABLE"], "intent_id": intent.intent_id, "client_order_id": client, "no_order_executed": True}
    plan = plan_pre_submit(intent=intent, context=context, risk_engine=RiskEngine(), venue=venue)
    return {"status": plan.risk_status, "reason_codes": list(plan.reason_codes), "intent_id": intent.intent_id, "client_order_id": client, "prepared_submission": dict(plan.prepared_submission or {}), "would_submit": plan.approved, "no_order_executed": True}
