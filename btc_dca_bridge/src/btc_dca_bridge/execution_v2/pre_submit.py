"""Shared pre-claim planning boundary for execution and read-only shadow."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..core.models import OrderIntent
from ..risk.engine import RiskContext, RiskEngine
from ..venues.base import PreSubmissionFailure, VenueAdapter


@dataclass(frozen=True)
class PreSubmitPlan:
    approved: bool
    risk_status: str
    reason_codes: tuple[str, ...]
    prepared_submission: Mapping[str, Any] | None = None


def plan_pre_submit(*, intent: OrderIntent, context: RiskContext, risk_engine: RiskEngine, venue: VenueAdapter) -> PreSubmitPlan:
    """Evaluate current risk and venue-native constraints before any claim/POST.

    This function owns no persistence and cannot cross the network-write
    boundary. Callers decide whether an approved plan stops (shadow) or first
    creates the durable OMS submission claim (production).
    """
    assessment = risk_engine.evaluate(intent, context)
    if not assessment.approved:
        return PreSubmitPlan(False, assessment.status, assessment.reason_codes)
    try:
        venue.validate_intent(intent)
        prepared = venue.prepare_submission(intent)
    except (PreSubmissionFailure, ValueError) as exc:
        return PreSubmitPlan(False, "BLOCKED", ("PRE_SUBMISSION_FAILURE", str(exc)))
    return PreSubmitPlan(True, assessment.status, assessment.reason_codes, prepared)
