"""Canonical production-shadow scheduler health policy.

GitHub scheduled events are best-effort.  A workflow API creation timestamp is
evidence of when GitHub delivered a scheduled run, not an identity for a cron
slot.  Health is therefore based on freshness of the latest scheduled main run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence

from ..config import RuntimeConfig, load_runtime_config


@dataclass(frozen=True)
class SchedulePolicy:
    cron: str
    cadence: timedelta
    lateness_tolerance: timedelta
    workflow_timeout: timedelta

    @property
    def freshness_slo(self) -> timedelta:
        return self.cadence + self.lateness_tolerance + self.workflow_timeout


def production_shadow_policy(runtime: RuntimeConfig | None = None) -> SchedulePolicy:
    runtime = runtime or load_runtime_config()
    return SchedulePolicy(
        cron=runtime.github_cron_utc,
        cadence=timedelta(hours=runtime.scheduled_interval_hours),
        lateness_tolerance=timedelta(minutes=runtime.scheduler_lateness_tolerance_minutes),
        workflow_timeout=timedelta(minutes=runtime.workflow_timeout_minutes),
    )


def system_health_crons(runtime: RuntimeConfig | None = None) -> tuple[str, str]:
    runtime = runtime or load_runtime_config()
    return runtime.system_health_critical_cron_utc, runtime.system_health_full_cron_utc


def utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("health timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def _run_created_at(run: Mapping[str, Any]) -> datetime:
    value = run.get("created_at")
    if not isinstance(value, str):
        raise ValueError("workflow run has no created_at")
    return utc(value)


def classify_production_shadow_freshness(
    observed_at: str | datetime,
    runs: Sequence[Mapping[str, Any]],
    *,
    policy: SchedulePolicy | None = None,
) -> dict[str, Any]:
    """Return scheduler health without pretending cron has exact delivery identity."""
    try:
        observed = utc(observed_at)
        policy = policy or production_shadow_policy()
        candidates: list[tuple[datetime, Mapping[str, Any]]] = []
        for run in runs:
            if not isinstance(run, Mapping):
                return {"state": "UNKNOWN", "run_id": None, "reason": "malformed production shadow evidence"}
            if run.get("event") != "schedule" or run.get("head_branch") != "main":
                continue
            created = _run_created_at(run)
            if created <= observed:
                candidates.append((created, run))
        if not candidates:
            return {"state": "ALERT", "run_id": None, "reason": "no scheduled main production shadow evidence", "freshness_slo_seconds": int(policy.freshness_slo.total_seconds())}
        created, run = max(candidates, key=lambda item: item[0])
        age = observed - created
        status, conclusion = run.get("status"), run.get("conclusion")
        base = {"run_id": run.get("id"), "created_at": created.isoformat().replace("+00:00", "Z"), "status": status, "conclusion": conclusion, "age_seconds": int(age.total_seconds()), "freshness_slo_seconds": int(policy.freshness_slo.total_seconds())}
        if status == "completed" and conclusion == "success":
            state = "HEALTHY" if age <= policy.freshness_slo else "ALERT"
            return {**base, "state": state, "reason": "latest scheduled main shadow succeeded within freshness SLO" if state == "HEALTHY" else "latest scheduled main shadow exceeds freshness SLO"}
        if status in {"queued", "in_progress"}:
            state = "DEGRADED" if age <= policy.freshness_slo else "ALERT"
            return {**base, "state": state, "reason": "latest scheduled main shadow is still running" if state == "DEGRADED" else "scheduled main shadow remains unfinished beyond freshness SLO"}
        if status == "completed" and conclusion in {"failure", "cancelled", "timed_out"}:
            return {**base, "state": "ALERT", "reason": f"latest scheduled main shadow completed with {conclusion}"}
        return {**base, "state": "UNKNOWN", "reason": "latest scheduled main shadow has unrecognized status"}
    except (TypeError, ValueError, KeyError) as exc:
        return {"state": "UNKNOWN", "run_id": None, "reason": f"production shadow evidence error: {exc}"}
