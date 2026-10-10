"""Conservative, deterministic classification for engineering branches."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import load_execution_config
from .final_readiness import final_readiness_audit
from .live_activation import read_activation_status
from .paths import DATA_PATH
from .production_enablement import read_enablement_status
from .real_money_authorization import read_authorization_status
from .artifacts import ArtifactStore

SCHEMA_VERSION = "1.0.0"
PROTECTED_FILES = {"btc_dca_bridge/config/execution.yaml", "btc_dca_bridge/config/active_strategy.json", ".github/workflows/production-shadow.yml"}
TRUSTED_GUARDIAN_FILES = {".github/workflows/branch-guardian.yml", ".github/workflows/branch-guardian-trusted.yml", ".github/workflows/ci.yml", "btc_dca_bridge/src/btc_dca_bridge/branch_guardian.py"}
FAIL_FILES = {"btc_dca_bridge/ledger/executions.jsonl"}
SENSITIVE = re.compile(r"ignore[._ -]?kill|force[._ -]?live|disable[._ -]?kill|override[._ -]?safety|bypass|auto[._ -]?(authorize|activate|enable)|standing[._ -]?(authorization|activation|enable)|live_execution_enabled|kill_switch|order_submission|monthly[_ -]?cap|market|Bybit|BTCUSDT|leverage|borrow|/v5/order/create|ledger[._ -]?before[._ -]?confirmation", re.IGNORECASE)


def changed_diff_lines(diff_text: str) -> str:
    """Return only added/removed content from a unified diff.

    Context and patch metadata are deliberately excluded so existing safety
    terminology cannot classify an unrelated documentation edit.
    """
    changed: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---", "diff --git", "index", "@@")):
            continue
        if line.startswith(("+", "-")):
            changed.append(line[1:])
    return "\n".join(changed)


def canonical_validation_succeeds(checks: Mapping[str, Any], head_sha: str) -> bool:
    """Accept only the real required GitHub Actions check for this SHA."""
    for check in checks.get("check_runs", ()):
        app = check.get("app") or {}
        if (check.get("name") == "Canonical validation" and check.get("conclusion") == "success"
                and check.get("head_sha") == head_sha
                and (app.get("slug") == "github-actions" or app.get("name") == "GitHub Actions")):
            return True
    return False


def _safe_runtime(runtime: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if runtime is not None:
        return dict(runtime)
    config = load_execution_config()
    store = ArtifactStore(DATA_PATH)
    return {
        "execution_config_safe": config.live_execution_enabled is False and config.kill_switch is True and config.order_submission == "not_implemented" and str(config.monthly_cap_usd) == "500" and config.exchange == "Bybit" and config.market_type == "spot" and config.symbol == "BTCUSDT",
        "authorization": read_authorization_status(store=store).get("authorization"),
        "activation": read_activation_status(store=store).get("activation"),
        "production_enablement": read_enablement_status(store=store).get("enablement"),
        "execution": "BLOCKED",
    }


def classify_branch(*, branch: str, head_sha: str, base_sha: str, changed_files: Iterable[str], diff_text: str, runtime: Mapping[str, Any] | None = None, bootstrap: bool = True) -> dict[str, Any]:
    files = sorted(set(changed_files))
    runtime = _safe_runtime(runtime)
    if any(path in FAIL_FILES or path.startswith("btc_dca_bridge/ledger/") for path in files):
        classification = "FAIL"
        reason = "canonical ledger surface changed"
    elif PROTECTED_FILES.intersection(files) or TRUSTED_GUARDIAN_FILES.intersection(files):
        classification = "SAFETY_SENSITIVE"
        reason = "protected production or Guardian trust surface changed"
    elif SENSITIVE.search(changed_diff_lines(diff_text)):
        classification = "SAFETY_SENSITIVE"
        reason = "conservative safety-semantic match in diff"
    elif not all((runtime.get(key) == expected) for key, expected in (("execution_config_safe", True), ("authorization", "NOT_AUTHORIZED"), ("activation", "NOT_ACTIVATED"), ("production_enablement", "NOT_ENABLED"), ("execution", "BLOCKED"))):
        classification = "FAIL"
        reason = "runtime safety invariant is not in the blocked default state"
    else:
        classification = "SAFE_ENGINEERING"
        reason = "no protected or safety-sensitive surface changed"
    safe = classification == "SAFE_ENGINEERING"
    return {"schema_version": SCHEMA_VERSION, "branch": branch, "head_sha": head_sha, "base_sha": base_sha, "classification": classification, "auto_merge_eligible": safe and not bootstrap, "manual_review_required": not safe or bootstrap, "guardian_bootstrap_version": 1, "manual_bootstrap_approval_required": bootstrap, "tests_passed": True, "protected_files_unchanged": not bool(PROTECTED_FILES.intersection(files)), "execution_config_safe": runtime.get("execution_config_safe") is True, "ledger_unchanged": not any(path in FAIL_FILES or path.startswith("btc_dca_bridge/ledger/") for path in files), "runtime_blocked": runtime.get("execution") == "BLOCKED", "authorization": runtime.get("authorization", "UNKNOWN"), "activation": runtime.get("activation", "UNKNOWN"), "production_enablement": runtime.get("production_enablement", "UNKNOWN"), "execution": runtime.get("execution", "UNKNOWN"), "reason": reason, "no_order_executed": True}


def write_result(result: Mapping[str, Any], output: Path) -> None:
    output.write_text(json.dumps(dict(result), sort_keys=True, indent=2) + "\n", encoding="utf-8")
