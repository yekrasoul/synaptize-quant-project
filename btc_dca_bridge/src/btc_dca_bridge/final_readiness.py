"""Read-only final production readiness audit.

This audit deliberately does not create a Bybit client.  Local evidence that
cannot be proven is reported as UNKNOWN and therefore cannot produce a ready
state.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore, ArtifactType
from .blocked_production import contract_status
from .config import load_execution_config, load_strategy_config
from .ledger import confirmed_executions, executions_for_month, read_executions
from .live_activation import read_activation_status
from .paths import DATA_PATH, LEDGER_PATH
from .production_enablement import read_enablement_status
from .real_money_authorization import read_authorization_status


def _check(state: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"state": state, "reason": reason, **extra}


def _latest_shadow(store: ArtifactStore) -> dict[str, Any]:
    root = store.root / "runs"
    paths = sorted(root.glob("*/*/*/*.json"))
    if not paths:
        return _check("UNKNOWN", "no local Production Shadow run evidence")
    path = paths[-1]
    parts = path.parts
    try:
        date = datetime(int(parts[-4]), int(parts[-3]), int(parts[-2]), tzinfo=UTC)
        payload = store.read(ArtifactType.RUN, run_id=path.stem, artifact_date_utc=date)
    except Exception as exc:
        return _check("UNKNOWN", f"Production Shadow evidence unavailable: {exc}")
    status = payload.get("status")
    return _check("HEALTHY" if status in {"completed", "success", "SUCCEEDED"} else "ALERT", f"latest local shadow status: {status}")


def final_readiness_audit(*, ledger_path: Path = LEDGER_PATH, data_root: Path = DATA_PATH,
                          now_utc: datetime | None = None) -> dict[str, Any]:
    now = (now_utc or datetime.now(UTC)).astimezone(UTC)
    checks: dict[str, Any] = {}
    try:
        strategy = load_strategy_config()
        checks["strategy"] = _check("HEALTHY", f"{strategy.strategy_id} {strategy.strategy_version}")
    except Exception as exc:
        checks["strategy"] = _check("CORRUPT", str(exc))
    try:
        history = read_executions(ledger_path)
        active = confirmed_executions(history)
        month = now.strftime("%Y-%m")
        month_rows = executions_for_month(history, month)
        spent = sum((Decimal(str(row.payload["executed_usd"])) for row in month_rows), Decimal("0"))
        checks["ledger"] = _check("HEALTHY", f"{len(history)} events; {len(active)} active confirmed executions", calendar_month=month, spent_usd=str(spent), remaining_usd=str(Decimal("500") - spent))
    except Exception as exc:
        checks["ledger"] = _check("CORRUPT", str(exc))
    store = ArtifactStore(data_root)
    checks["production_shadow"] = _latest_shadow(store)
    checks["canonical_ci"] = _check("UNKNOWN", "Canonical CI evidence is not locally available")
    try:
        contract = contract_status(now=now)
        capabilities = contract["capabilities"]
        checks["production_contract"] = _check("HEALTHY", "read-only contract assumptions loaded", capabilities=capabilities)
        checks["bybit_spot_contract"] = _check("HEALTHY", "Bybit Spot BTCUSDT contract is exact")
    except Exception as exc:
        checks["production_contract"] = _check("UNKNOWN", str(exc))
        checks["bybit_spot_contract"] = _check("UNKNOWN", str(exc))
    for name, reason in (
        ("account_identity", "no local current account identity evidence"),
        ("credential_scope", "no local current credential scope evidence"),
        ("withdrawal_permission", "no local current withdrawal permission evidence"),
        ("liabilities", "no local current liability evidence"),
        ("spot_usdt_availability", "no local authoritative Spot USDT availability evidence"),
        ("instrument_metadata", "no local current instrument metadata evidence"),
        ("clock_evidence", "no local current clock evidence"),
    ):
        checks[name] = _check("UNKNOWN", reason)
    checks["reconciliation_path"] = _check("HEALTHY", "local reconciliation implementation is available")
    attempt_exists = bool(list((data_root / "order_submission_attempts").glob("*/*/*/*.json")) or list((data_root / "live_execution_attempts").glob("*/*/*/*.json")))
    checks["unresolved_submission_attempts"] = _check("RECONCILIATION_REQUIRED" if attempt_exists else "HEALTHY", "submission-attempt evidence exists" if attempt_exists else "no local submission-attempt artifacts found")
    checks["authorization"] = _check(read_authorization_status(store=store).get("authorization", "UNKNOWN"), "canonical authorization status")
    checks["activation"] = _check(read_activation_status(store=store).get("activation", "UNKNOWN"), "canonical activation status")
    checks["production_enablement"] = _check(read_enablement_status(store=store).get("enablement", "UNKNOWN"), "canonical production enablement status")
    try:
        config = load_execution_config()
        safe = config.live_execution_enabled is False and config.kill_switch is True and config.order_submission == "not_implemented" and config.monthly_cap_usd == Decimal("500")
        checks["execution_config"] = _check("HEALTHY" if safe else "CRITICAL", "checked-in execution safety policy", live_execution_enabled=config.live_execution_enabled, kill_switch=config.kill_switch, order_submission=config.order_submission, monthly_cap_usd=str(config.monthly_cap_usd), market="Bybit Spot BTCUSDT")
    except Exception as exc:
        checks["execution_config"] = _check("CORRUPT", str(exc))
    critical = {"ledger", "execution_config"}
    if any(checks[name]["state"] in {"CORRUPT", "CRITICAL"} for name in critical):
        readiness = "CORRUPT"
    elif checks["unresolved_submission_attempts"]["state"] != "HEALTHY":
        readiness = "RECONCILIATION_REQUIRED"
    else:
        readiness = "BLOCKED"
    config = checks["execution_config"]
    return {
        "readiness": readiness,
        "production_enablement": checks["production_enablement"]["state"],
        "authorization": checks["authorization"]["state"],
        "activation": checks["activation"]["state"],
        "execution": "BLOCKED",
        "kill_switch": config.get("kill_switch", True),
        "live_execution_enabled": config.get("live_execution_enabled", False),
        "order_submission": config.get("order_submission", "not_implemented"),
        "real_money_execution_possible": False,
        "checks": checks,
        "message": "NO ORDER EXECUTED",
    }
