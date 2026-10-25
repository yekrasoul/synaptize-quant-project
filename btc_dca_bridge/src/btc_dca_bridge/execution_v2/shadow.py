"""Read-only V2 production-equivalent preflight for Production Shadow."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from ..core.models import OrderIntent
from ..risk.engine import RiskContext, RiskEngine
from ..venues.base import VenueAdapter
from .pre_submit import plan_pre_submit
from .composition import V2ExecutionRuntime, order_intent_from_decision


def plan_v2_shadow(decision: Mapping[str, Any], *, run_id: str, created_at_utc: str, monthly_spend: Decimal, context: RiskContext | None = None, venue: VenueAdapter | None = None, runtime: V2ExecutionRuntime | None = None) -> dict[str, Any]:
    amount = Decimal(str(decision["final_purchase_usd"]))
    if amount <= 0:
        return {"status": "NO_INTENT", "no_order_executed": True}
    if runtime is not None:
        return runtime.plan_shadow(
            decision,
            run_id=run_id,
            created_at_utc=created_at_utc,
            context=context,
        )
    intent = order_intent_from_decision(
        decision, run_id=run_id, created_at_utc=created_at_utc
    )
    # Do not fabricate authenticated account, venue-health, or runtime policy
    # evidence. A production shadow without those safe inputs reports its
    # pre-submit parity as unverified rather than pretending to be blocked.
    if context is None or venue is None:
        return {"status": "NOT_VERIFIED", "reason_codes": ["FRESH_ACCOUNT_AND_VENUE_CONTEXT_UNAVAILABLE"], "intent_id": intent.intent_id, "client_order_id": intent.client_order_id, "no_order_executed": True}
    plan = plan_pre_submit(intent=intent, context=context, risk_engine=RiskEngine(), venue=venue)
    return {"status": plan.risk_status, "reason_codes": list(plan.reason_codes), "intent_id": intent.intent_id, "client_order_id": intent.client_order_id, "prepared_submission": dict(plan.prepared_submission or {}), "would_submit": plan.approved, "no_order_executed": True}
