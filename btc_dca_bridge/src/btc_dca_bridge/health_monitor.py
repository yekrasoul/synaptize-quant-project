"""Deterministic, read-only policy for the unified system health monitor."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

CRITICAL_CRON = "17 * * * *"
FULL_CRON = "43 */6 * * *"
PRODUCTION_SHADOW_CRON = "23 */6 * * *"
CRITICAL_CHECKS = (
    "self_hosted_runner", "production_shadow", "watchdog", "canonical_ci",
    "artifacts", "ledger_integrity", "telegram",
)
FULL_CHECKS = CRITICAL_CHECKS + (
    "artifact_consistency", "bybit_spot_connectivity", "market_data_freshness", "production_status",
    "external_dependencies", "schedule_drift", "infrastructure",
)
SAFETY_EXPECTATIONS = {
    "live_execution_enabled": False,
    "kill_switch": True,
    "order_submission": "not_implemented",
    "authorization": "NOT_AUTHORIZED",
}
HEALTHY_STATES = {"HEALTHY", "OK", "PASS", "SUCCESS"}
ALERTING_STATES = {"ALERT", "CRITICAL", "FAILURE", "FAILED"}
NON_ALERTING_STATES = {"INITIALIZING", "CONFIGURED", "NOT_APPLICABLE", "NOT_CONFIGURED", "UNKNOWN", "NOT_RUN"}


def _utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("health timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def derive_mode(*, event_name: str, schedule: str | None, requested_mode: str | None) -> str:
    if event_name == "schedule":
        if schedule == CRITICAL_CRON:
            return "critical"
        if schedule == FULL_CRON:
            return "full"
        raise ValueError(f"unsupported monitoring schedule: {schedule!r}")
    if event_name == "workflow_dispatch" and requested_mode in {"critical", "full"}:
        return requested_mode
    raise ValueError("monitoring mode/event is invalid")


def expected_production_slot(observed_at: str | datetime) -> datetime:
    """Return the prior production slot, preserving the watchdog's strict-before rule."""
    observed = _utc(observed_at)
    day = observed.replace(hour=0, minute=23, second=0, microsecond=0)
    slots = [day + timedelta(hours=hour) for hour in (0, 6, 12, 18)]
    prior = [slot for slot in slots if slot < observed]
    return prior[-1] if prior else day - timedelta(hours=6)


def _parse_run_time(run: Mapping[str, Any]) -> datetime:
    value = run.get("created_at")
    if not isinstance(value, str):
        raise ValueError("workflow run has no created_at")
    return _utc(value)


def classify_production_shadow(observed_at: str | datetime, runs: Sequence[Mapping[str, Any]], *, grace_minutes: int = 60) -> dict[str, Any]:
    """Classify only the expected scheduled main production-shadow run."""
    try:
        observed = _utc(observed_at)
        slot = expected_production_slot(observed)
        grace_end = slot + timedelta(minutes=grace_minutes)
        candidates = []
        for run in runs:
            if run.get("event") != "schedule" or run.get("head_branch") != "main":
                continue
            created = _parse_run_time(run)
            if slot <= created < grace_end:
                candidates.append((created, run))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            return {"state": "ALERT", "expected_slot": slot.isoformat().replace("+00:00", "Z"), "run_id": None, "reason": "missing expected scheduled production shadow"}
        created, run = candidates[0]
        status, conclusion = run.get("status"), run.get("conclusion")
        if status == "completed" and conclusion == "success":
            state = "HEALTHY"
        elif status in {"queued", "in_progress"}:
            state = "ALERT"
        elif status == "completed" and conclusion in {"failure", "cancelled", "timed_out"}:
            state = "ALERT"
        else:
            state = "UNKNOWN"
        return {"state": state, "expected_slot": slot.isoformat().replace("+00:00", "Z"), "run_id": run.get("id"), "created_at": created.isoformat().replace("+00:00", "Z"), "status": status, "conclusion": conclusion, "reason": f"scheduled main run is {status}/{conclusion}"}
    except (TypeError, ValueError, KeyError) as exc:
        return {"state": "UNKNOWN", "expected_slot": None, "run_id": None, "reason": f"production shadow evidence error: {exc}"}


def classify_self_hosted_runner(shadow: Mapping[str, Any], jobs_by_run_id: Mapping[str, Any]) -> str:
    """Use only runner evidence belonging to the exact matched shadow run."""
    shadow_state = shadow.get("state")
    if shadow_state != "HEALTHY":
        return shadow_state if shadow_state in {"ALERT", "UNKNOWN"} else "UNKNOWN"
    run_id = shadow.get("run_id")
    if run_id is None or not isinstance(jobs_by_run_id, Mapping):
        return "UNKNOWN"
    jobs = jobs_by_run_id.get(str(run_id))
    if not isinstance(jobs, list):
        return "UNKNOWN"
    for job in jobs:
        if not isinstance(job, Mapping) or str(job.get("run_id")) != str(run_id):
            return "UNKNOWN"
    if any(isinstance(job.get("runner_name"), str) and job["runner_name"].strip() for job in jobs):
        return "HEALTHY"
    return "UNKNOWN"


def classify_watchdog(
    observed_at: str | datetime,
    runs: Sequence[Mapping[str, Any]],
    *,
    current_run_id: str | int | None = None,
    freshness_hours: int = 2,
) -> str:
    """Classify monitor freshness without turning an installation's first run into an incident."""
    try:
        observed = _utc(observed_at)
        current_id = str(current_run_id) if current_run_id is not None else None
        valid_runs: list[tuple[datetime, Mapping[str, Any]]] = []
        malformed = False
        for run in runs:
            if not isinstance(run, Mapping):
                malformed = True
                continue
            if current_id is not None and str(run.get("id")) == current_id:
                continue
            event = run.get("event")
            head_branch = run.get("head_branch")
            if event != "schedule" or head_branch != "main":
                continue
            try:
                valid_runs.append((_parse_run_time(run), run))
            except (AttributeError, TypeError, ValueError, KeyError):
                malformed = True
        if malformed:
            return "UNKNOWN"
        recent_cutoff = observed - timedelta(hours=freshness_hours)
        recent = [(created, run) for created, run in valid_runs if recent_cutoff <= created <= observed]
        if any(run.get("status") == "completed" and run.get("conclusion") == "success" for _, run in recent):
            return "HEALTHY"
        if any(
            run.get("status") in {"queued", "in_progress"}
            or run.get("status") == "completed" and run.get("conclusion") in {"failure", "cancelled", "timed_out"}
            for _, run in recent
        ):
            return "ALERT"
        if any(run.get("status") == "completed" and run.get("conclusion") == "success" for _, run in valid_runs):
            return "ALERT"
        return "INITIALIZING"
    except (TypeError, ValueError, KeyError):
        return "UNKNOWN"


def classify_canonical_ci(runs: Sequence[Mapping[str, Any]]) -> str:
    """Classify CI under its pull-request/manual trigger model, without inventing push evidence."""
    relevant = []
    for run in runs:
        if not isinstance(run, Mapping):
            continue
        if run.get("event") == "pull_request":
            relevant.append(run)
        elif run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main":
            relevant.append(run)
        elif run.get("event") is None and run.get("head_branch") == "main":
            relevant.append(run)
    if not relevant:
        return "UNKNOWN"
    try:
        latest = max(relevant, key=_parse_run_time)
        if latest.get("status") == "completed" and latest.get("conclusion") == "success":
            return "HEALTHY"
        if latest.get("conclusion") in {"failure", "cancelled", "timed_out"}:
            return "ALERT"
        return "UNKNOWN"
    except (TypeError, ValueError, KeyError):
        return "UNKNOWN"


def canonical_contract_state(contract: Mapping[str, Any], blockers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    capabilities = contract.get("capabilities", {})
    authorization = contract.get("real_money_authorization", {})
    stable = {
        "blocker_ids": sorted(str(item.get("blocker_id")) for item in blockers),
        "production_quote_limit_conclusion": contract.get("production_quote_limit_conclusion"),
        "approved_quote_unit_limit_source_count": contract.get("approved_quote_unit_limit_source_count"),
        "quote_unit_maximum_supported": capabilities.get("quote_unit_maximum_supported"),
        "quote_unit_maximum_source": capabilities.get("quote_unit_maximum_source"),
        "spot_quote_availability_supported": capabilities.get("spot_quote_availability_supported"),
        "market_unit_quote_coin_supported": capabilities.get("market_unit_quote_coin_supported"),
        "real_money_authorization_status": authorization.get("status", "UNKNOWN"),
    }
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"state": "HEALTHY" if not stable["blocker_ids"] else "ALERT", "fingerprint": hashlib.sha256(canonical.encode()).hexdigest(), "details": stable}


def classify_production_status(payload: Mapping[str, Any] | None) -> str:
    if not payload:
        return "UNKNOWN"
    if payload.get("canonical_ledger_status") in {"CORRUPT", "INVALID"} or payload.get("overall_operator_state") == "CORRUPT":
        return "ALERT"
    if payload.get("canonical_ledger_status") == "VALID" and payload.get("software_health") == "HEALTHY" and payload.get("overall_operator_state") in {"HEALTHY", "NO_ACTIVE_EXTERNAL_BLOCKERS"}:
        return "HEALTHY"
    return "DEGRADED"


def classify_connectivity_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Classify connectivity while retaining only safe, read-only diagnostics."""
    safe_keys = (
        "status", "read_only", "message", "observed_at_utc", "reason", "error",
        "error_category", "http_status", "endpoint", "source", "blocker",
    )
    if not isinstance(payload, Mapping):
        return {"state": "UNKNOWN", "reason": "missing or unparseable production-connectivity output", "raw": None}
    safe: dict[str, Any] = {key: payload[key] for key in safe_keys if key in payload and not isinstance(payload[key], (Mapping, list))}
    if isinstance(payload.get("endpoints"), list):
        safe["endpoints"] = []
        endpoint_keys = ("endpoint", "status", "reason", "read_only", "observed_at_utc", "response_contract_valid", "http_status", "error_category")
        for endpoint in payload["endpoints"]:
            if isinstance(endpoint, Mapping):
                safe["endpoints"].append({key: endpoint[key] for key in endpoint_keys if key in endpoint and not isinstance(endpoint[key], (Mapping, list))})
    status = payload.get("status")
    diagnostic_text = " ".join(str(value) for value in (payload.get("reason"), payload.get("error"), payload.get("message")) if value)
    diagnostic_text += " " + " ".join(str(item.get("reason")) for item in payload.get("endpoints", []) if isinstance(item, Mapping) and item.get("reason"))
    missing_credentials = status == "READS_UNAVAILABLE" and "bybit_api_key and bybit_api_secret must be set" in diagnostic_text.lower()
    if missing_credentials:
        return {"state": "NOT_CONFIGURED", "reason": next((item.get("reason") for item in payload.get("endpoints", []) if isinstance(item, Mapping) and item.get("reason")), payload.get("reason") or payload.get("message") or "Bybit credentials are not configured"), "raw": safe}
    if status in {"READS_OK", "HEALTHY", "PASS"}:
        state = "HEALTHY"
        reason = "read-only connectivity checks succeeded"
    elif status in {"READS_FAILED", "READS_UNAVAILABLE", "ALERT", "FAILURE", "FAILED"}:
        state = "ALERT"
        failed_endpoint = next((item for item in payload.get("endpoints", []) if isinstance(item, Mapping) and item.get("status") not in {None, "PASS", "HEALTHY"}), None)
        reason = payload.get("reason") or payload.get("error") or (failed_endpoint or {}).get("reason") or payload.get("message") or "read-only connectivity checks failed"
    else:
        state = "UNKNOWN"
        reason = payload.get("message") or payload.get("reason") or "connectivity result has no recognized status"
    return {"state": state, "reason": str(reason), "raw": safe}


def classify_market_data_artifacts_detailed(root: str | Path, observed_at: str | datetime, *, max_age_hours: int = 8) -> dict[str, Any]:
    """Classify matching Bybit Spot BTCUSDT evidence and explain the result."""
    latest: tuple[datetime, Mapping[str, Any]] | None = None
    malformed = False
    unreadable = False
    try:
        for path in Path(root).rglob("*.json"):
            if path.name.endswith(".sha256"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except OSError:
                unreadable = True
                continue
            except (TypeError, ValueError, json.JSONDecodeError):
                malformed = True
                continue
            try:
                captured = payload.get("captured_at_utc")
                if not captured:
                    continue
                timestamp = _utc(captured)
                if payload.get("source_exchange") != "Bybit" or payload.get("market_type") != "spot" or payload.get("symbol") != "BTCUSDT":
                    continue
                if latest is None or timestamp > latest[0]:
                    latest = (timestamp, payload)
            except (TypeError, ValueError, KeyError):
                malformed = True
                continue
        if latest is None:
            reason = "malformed/unreadable evidence" if malformed or unreadable else "no matching Bybit Spot BTCUSDT market snapshot"
            return {"state": "UNKNOWN", "latest_captured_at_utc": None, "age_seconds": None, "max_age_hours": max_age_hours, "fresh": None, "source_exchange": None, "market_type": None, "symbol": None, "reason": reason}
        age = _utc(observed_at) - latest[0]
        details = {"state": "UNKNOWN", "latest_captured_at_utc": latest[0].isoformat().replace("+00:00", "Z"), "age_seconds": int(age.total_seconds()), "max_age_hours": max_age_hours, "fresh": latest[1].get("fresh"), "source_exchange": latest[1].get("source_exchange"), "market_type": latest[1].get("market_type"), "symbol": latest[1].get("symbol"), "reason": ""}
        if age < timedelta(0):
            details["state"], details["reason"] = "ALERT", "snapshot timestamp is in the future"
        elif age > timedelta(hours=max_age_hours):
            details["state"], details["reason"] = "ALERT", f"latest matching snapshot age {_format_age(age)} exceeds max {max_age_hours}h"
        elif latest[1].get("fresh") is not True:
            details["state"], details["reason"] = "ALERT", "snapshot has fresh != true"
        else:
            details["state"], details["reason"] = "HEALTHY", "healthy fresh snapshot"
        return details
    except (OSError, TypeError, ValueError):
        return {"state": "UNKNOWN", "latest_captured_at_utc": None, "age_seconds": None, "max_age_hours": max_age_hours, "fresh": None, "source_exchange": None, "market_type": None, "symbol": None, "reason": "malformed/unreadable evidence"}


def classify_market_data_artifacts(root: str | Path, observed_at: str | datetime, *, max_age_hours: int = 8) -> str:
    return str(classify_market_data_artifacts_detailed(root, observed_at, max_age_hours=max_age_hours)["state"])


def _format_age(age: timedelta) -> str:
    total_seconds = max(0, int(age.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes:02d}m"


def classify_artifact_integrity(root: str | Path) -> str:
    json_files = [path for path in Path(root).rglob("*.json") if not path.name.endswith(".sha256")]
    if not json_files:
        return "UNKNOWN"
    for path in json_files:
        digest_path = path.with_suffix(path.suffix + ".sha256")
        if not digest_path.is_file():
            return "UNKNOWN"
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest_path.read_text(encoding="utf-8").strip() != actual:
                return "ALERT"
        except OSError:
            return "UNKNOWN"
    return "HEALTHY"


def authorization_from_canonical(*sources: Mapping[str, Any] | None) -> str:
    values: list[str] = []
    for source in sources:
        if not source:
            continue
        for key in ("real_money_authorization", "authorization"):
            value = source.get(key)
            if isinstance(value, Mapping) and isinstance(value.get("status"), str):
                values.append(value["status"])
            if isinstance(value, str):
                values.append(value)
    if not values or len(set(values)) != 1:
        return "UNKNOWN"
    return values[0]


def safety_violations(safety: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(key for key, expected in SAFETY_EXPECTATIONS.items() if safety.get(key) != expected)


def _healthy(value: Any) -> bool:
    return str(value).upper() in HEALTHY_STATES


def _alerting(value: Any) -> bool:
    return str(value).upper() in ALERTING_STATES


def _non_alerting_startup(value: Any) -> bool:
    return str(value).upper() in NON_ALERTING_STATES


def _fingerprint(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def evaluate(*, mode: str, checks: Mapping[str, Any], safety: Mapping[str, Any], observed_at: str, previous: Mapping[str, Any] | None = None, send_test_alert: bool = False, send_test_summary: bool = False) -> dict[str, Any]:
    if mode not in {"critical", "full"}:
        raise ValueError("mode must be critical or full")
    relevant = CRITICAL_CHECKS if mode == "critical" else FULL_CHECKS
    components = {name: str(checks.get(name, "NOT_RUN")).upper() for name in relevant}
    violations = safety_violations(safety)
    if violations or any(_alerting(components[name]) for name in CRITICAL_CHECKS):
        critical_overall = "CRITICAL"
    elif all(_healthy(components[name]) for name in CRITICAL_CHECKS):
        critical_overall = "HEALTHY"
    else:
        critical_overall = "UNKNOWN"
    if critical_overall == "CRITICAL":
        overall = "CRITICAL"
    elif mode == "full" and any(_alerting(components[name]) for name in FULL_CHECKS):
        overall = "DEGRADED"
    elif all(_healthy(components[name]) for name in relevant):
        overall = "HEALTHY"
    else:
        overall = "UNKNOWN"

    previous = previous or {}
    delivered_alerts = dict(previous.get("delivered_alerts", {}))
    fingerprints = dict(checks.get("fingerprints", {}))
    pending: list[dict[str, str]] = []
    test_mode = send_test_alert or send_test_summary
    if test_mode:
        if send_test_alert:
            pending.append({"kind": "TEST_ALERT", "component": "system", "fingerprint": _fingerprint(observed_at)})
    else:
        for violation in violations:
            name = f"safety.{violation}"
            fp = _fingerprint({"violation": violation, "value": safety.get(violation)})
            if delivered_alerts.get(name) != fp:
                pending.append({"kind": "CRITICAL", "component": name, "fingerprint": fp})
        for name in relevant:
            state = components[name]
            fp = str(fingerprints.get(name, _fingerprint(state)))
            if _alerting(state) and delivered_alerts.get(name) != fp:
                pending.append({"kind": "CRITICAL" if name in CRITICAL_CHECKS else "WARNING", "component": name, "fingerprint": fp})
            if _healthy(state) and name in delivered_alerts:
                pending.append({"kind": "RECOVERED", "component": name, "fingerprint": delivered_alerts[name]})
        contract = checks.get("contract_state")
        previous_contract = previous.get("contract_state")
        if mode == "full" and isinstance(contract, Mapping) and isinstance(previous_contract, Mapping) and contract.get("fingerprint") != previous_contract.get("fingerprint") and previous.get("delivered_contract_fingerprint") != contract.get("fingerprint"):
            fp = str(contract.get("fingerprint", _fingerprint(contract)))
            if not any(item["component"] == "external_dependencies" for item in pending):
                pending.append({"kind": "CONTRACT_CHANGE", "component": "external_dependencies", "fingerprint": fp})

    observed_date = _utc(observed_at).date().isoformat()
    summary_due = send_test_summary if test_mode else previous.get("summary_delivery_date") != observed_date
    return {"schema_version": "2.0.0", "mode": mode, "observed_at_utc": _utc(observed_at).isoformat().replace("+00:00", "Z"), "overall": overall, "critical_monitor": critical_overall, "components": components, "fingerprints": fingerprints, "safety": dict(safety), "safety_violations": list(violations), "pending_notifications": pending, "daily_summary_due": summary_due, "test_mode": test_mode, "diagnostics": checks.get("diagnostics", {}), "contract_state": checks.get("contract_state")}


def commit_state(result: Mapping[str, Any], previous: Mapping[str, Any] | None, *, alerts_delivered: bool, summary_delivered: bool) -> dict[str, Any]:
    """Advance notification acknowledgements only after confirmed delivery."""
    previous = previous or {}
    if result.get("test_mode"):
        return dict(previous)
    state = {"schema_version": "2.0.0", "observed_at_utc": result["observed_at_utc"], "overall": result["overall"], "components": dict(result.get("components", {})), "fingerprints": dict(result.get("fingerprints", {})), "safety": dict(result.get("safety", {})), "safety_violations": list(result.get("safety_violations", [])), "contract_state": result.get("contract_state"), "delivered_alerts": dict(previous.get("delivered_alerts", {})), "delivered_recoveries": dict(previous.get("delivered_recoveries", {})), "delivered_contract_fingerprint": previous.get("delivered_contract_fingerprint"), "summary_delivery_date": previous.get("summary_delivery_date"), "last_telegram_delivery_at": previous.get("last_telegram_delivery_at")}
    if alerts_delivered:
        for item in result.get("pending_notifications", []):
            if item["kind"] in {"CRITICAL", "WARNING"}:
                state["delivered_alerts"][item["component"]] = item["fingerprint"]
                state["delivered_recoveries"].pop(item["component"], None)
            elif item["kind"] == "CONTRACT_CHANGE":
                state["delivered_contract_fingerprint"] = item["fingerprint"]
            elif item["kind"] == "RECOVERED":
                state["delivered_alerts"].pop(item["component"], None)
                state["delivered_recoveries"][item["component"]] = item["fingerprint"]
    if summary_delivered:
        state["summary_delivery_date"] = _utc(result["observed_at_utc"]).date().isoformat()
    if alerts_delivered or summary_delivered:
        state["last_telegram_delivery_at"] = result["observed_at_utc"]
    return state


def render_summary(result: Mapping[str, Any]) -> str:
    components, safety = result.get("components", {}), result.get("safety", {})
    diagnostics = result.get("diagnostics", {})
    lines = ["BTC DCA SYSTEM HEALTH", "", f"Overall: {result.get('overall', 'UNKNOWN')}", f"Observed: {result.get('observed_at_utc', 'UNKNOWN')}", "", f"Critical Monitor: {result.get('critical_monitor', 'UNKNOWN')}", f"Production Shadow: {components.get('production_shadow', 'UNKNOWN')}", f"Self-hosted Runner: {components.get('self_hosted_runner', 'UNKNOWN')}", f"Watchdog: {components.get('watchdog', 'UNKNOWN')}", f"Canonical CI: {components.get('canonical_ci', 'UNKNOWN')}", f"Bybit Spot Connectivity: {components.get('bybit_spot_connectivity', 'NOT_RUN')}", f"Market-data freshness: {components.get('market_data_freshness', 'NOT_RUN')}", f"Artifacts: {components.get('artifacts', 'UNKNOWN')}", f"Ledger Integrity: {components.get('ledger_integrity', 'UNKNOWN')}"]
    for component, label in (("bybit_spot_connectivity", "Bybit Spot Connectivity"), ("market_data_freshness", "Market-data freshness"), ("watchdog", "Watchdog"), ("canonical_ci", "Canonical CI")):
        detail = diagnostics.get(component)
        if isinstance(detail, Mapping) and detail.get("reason"):
            lines.append(f"  {label} diagnostic:")
            raw = detail.get("raw")
            if component == "bybit_spot_connectivity" and isinstance(raw, Mapping) and raw.get("status") is not None:
                lines.append(f"    Status: {raw['status']}")
                endpoint = next((item for item in raw.get("endpoints", []) if isinstance(item, Mapping) and item.get("endpoint")), None)
                if endpoint:
                    lines.append(f"    Endpoint: {endpoint['endpoint']}")
                    if endpoint.get("http_status") is not None:
                        lines.append(f"    HTTP status: {endpoint['http_status']}")
            if component == "market_data_freshness":
                if detail.get("latest_captured_at_utc"):
                    lines.append(f"    Latest snapshot: {detail['latest_captured_at_utc']}")
                if detail.get("age_seconds") is not None:
                    lines.append(f"    Age: {_format_age(timedelta(seconds=int(detail['age_seconds'])))}")
                if detail.get("fresh") is not None:
                    lines.append(f"    Fresh flag: {str(detail['fresh']).lower()}")
            lines.append(f"    Reason: {detail['reason']}")
    lines.extend(["", "Safety:", f"live_execution_enabled: {str(safety.get('live_execution_enabled')).lower()}", f"kill_switch: {str(safety.get('kill_switch')).lower()}", f"order_submission: {safety.get('order_submission', 'UNKNOWN')}", f"Authorization: {safety.get('authorization', 'UNKNOWN')}", "", "NO ORDER EXECUTED"])
    return "\n".join(lines)


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evaluate", type=Path)
    parser.add_argument("--commit", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--alerts-delivered", action="store_true")
    parser.add_argument("--summary-delivered", action="store_true")
    args = parser.parse_args()
    if args.evaluate:
        print(json.dumps(evaluate(**json.loads(args.evaluate.read_text(encoding="utf-8"))), sort_keys=True))
        return 0
    if args.commit and args.output:
        previous = json.loads(args.previous.read_text(encoding="utf-8")) if args.previous and args.previous.is_file() else None
        args.output.write_text(json.dumps(commit_state(json.loads(args.commit.read_text(encoding="utf-8")), previous, alerts_delivered=args.alerts_delivered, summary_delivered=args.summary_delivered), sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return 0
    parser.error("use --evaluate or --commit with --output")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
