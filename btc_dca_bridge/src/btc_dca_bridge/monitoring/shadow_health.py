"""CLI adapter for the canonical, read-only production-shadow scheduler policy."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .schedule import classify_production_shadow_freshness


def evaluate_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    observed_at = payload.get("observed_at")
    runs = payload.get("shadow_runs")
    if not isinstance(observed_at, str) or not isinstance(runs, list):
        return {"monitor_execution_status": "FAILED", "scheduler_health_status": "UNKNOWN", "system_health_status": "UNKNOWN", "reason": "required GitHub evidence is malformed"}
    health = classify_production_shadow_freshness(observed_at, runs)
    return {
        "monitor_execution_status": "SUCCESS",
        "scheduler_health_status": health["state"],
        "system_health_status": health["state"],
        **health,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.evidence.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        result = {"monitor_execution_status": "FAILED", "scheduler_health_status": "UNKNOWN", "system_health_status": "UNKNOWN", "reason": f"GitHub evidence unavailable: {exc}"}
    else:
        result = evaluate_evidence(payload) if isinstance(payload, dict) else {"monitor_execution_status": "FAILED", "scheduler_health_status": "UNKNOWN", "system_health_status": "UNKNOWN", "reason": "GitHub evidence must be an object"}
    print(json.dumps(result, sort_keys=True))
    output = args.github_output or Path(os.environ["GITHUB_OUTPUT"])
    with output.open("a", encoding="utf-8") as handle:
        for name in ("monitor_execution_status", "scheduler_health_status", "system_health_status", "run_id", "status", "conclusion", "reason"):
            value = result.get(name, "unknown")
            handle.write(f"{name}={value if value is not None else 'none'}\n")
    return 0 if result["monitor_execution_status"] == "SUCCESS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
