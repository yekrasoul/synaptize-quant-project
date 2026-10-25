"""The single authoritative current-generation runtime execution policy."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class RuntimeExecutionPolicy:
    execution_enabled: bool
    kill_switch_active: bool
    monthly_cap_usd: Decimal
    allowed_venue: str
    allowed_market: str
    expected_account_fingerprint: str


def load_runtime_execution_policy(path: Path) -> RuntimeExecutionPolicy:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"runtime execution policy is unreadable: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"execution_enabled", "kill_switch_active", "monthly_cap_usd", "allowed_venue", "allowed_market", "expected_account_fingerprint"}:
        raise ValueError("runtime execution policy keys are invalid")
    if not isinstance(raw["execution_enabled"], bool) or not isinstance(raw["kill_switch_active"], bool):
        raise ValueError("runtime execution policy switches must be boolean")
    cap = Decimal(str(raw["monthly_cap_usd"]))
    if cap != Decimal("500"):
        raise ValueError("monthly cap must remain 500 USD")
    if raw["allowed_market"] != "BTC/USDT:spot" or raw["allowed_venue"] != "bybit":
        raise ValueError("current production market policy is Bybit BTC/USDT Spot")
    return RuntimeExecutionPolicy(raw["execution_enabled"], raw["kill_switch_active"], cap, raw["allowed_venue"], raw["allowed_market"], str(raw["expected_account_fingerprint"]))


@dataclass(frozen=True)
class V2ActivationGate:
    allowed: bool
    reason_codes: tuple[str, ...]


def load_v2_activation_gate(path: Path) -> V2ActivationGate:
    """Fail closed unless the canonical V2 checklist is unanimously VERIFIED."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"V2 activation checklist is unreadable: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("requirements"), dict):
        raise ValueError("V2 activation checklist is malformed")
    reasons: list[str] = []
    if raw.get("activation_decision") != "READY":
        reasons.append("ACTIVATION_DECISION_NOT_READY")
    for name, status in raw["requirements"].items():
        if status != "VERIFIED":
            reasons.append(f"{str(name).upper()}_{str(status)}")
    return V2ActivationGate(not reasons, tuple(reasons))
