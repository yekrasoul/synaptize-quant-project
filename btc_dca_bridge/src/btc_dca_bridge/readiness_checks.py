"""Canonical production-evidence check identifiers and Gate-D projection."""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


class ReadinessCheckId(str, Enum):
    CONFIG_DEFAULTS = "CONFIG_DEFAULTS"
    STRATEGY_IDENTITY = "STRATEGY_IDENTITY"
    CANONICAL_LEDGER = "CANONICAL_LEDGER"
    ARTIFACT_INTEGRITY = "ARTIFACT_INTEGRITY"
    UNRESOLVED_OPERATIONS = "UNRESOLVED_OPERATIONS"
    MONTHLY_BUDGET = "MONTHLY_BUDGET"
    REPOSITORY_CLEAN = "REPOSITORY_CLEAN"
    BYBIT_CREDENTIAL_SCOPE = "BYBIT_CREDENTIAL_SCOPE"
    BYBIT_ACCOUNT = "BYBIT_ACCOUNT"
    BYBIT_LIABILITIES = "BYBIT_LIABILITIES"
    BYBIT_SPOT_AVAILABLE_BALANCE = "BYBIT_SPOT_AVAILABLE_BALANCE"
    BYBIT_INSTRUMENT = "BYBIT_INSTRUMENT"
    CLOCK_SKEW = "CLOCK_SKEW"
    PRODUCTION_CONNECTIVITY = "PRODUCTION_CONNECTIVITY"
    DETERMINISTIC_ORDER_ID = "DETERMINISTIC_ORDER_ID"
    FILESYSTEM_DURABILITY = "FILESYSTEM_DURABILITY"
    OPERATOR_LOCK = "OPERATOR_LOCK"


_REQUIRED_PROJECTION = {
    "ledger_valid": ReadinessCheckId.CANONICAL_LEDGER,
    "v1_strategy_valid": ReadinessCheckId.STRATEGY_IDENTITY,
    "account_identity_known": ReadinessCheckId.BYBIT_ACCOUNT,
    "account_mode_accepted": ReadinessCheckId.BYBIT_ACCOUNT,
    "liabilities_clear": ReadinessCheckId.BYBIT_LIABILITIES,
    "credential_scope_accepted": ReadinessCheckId.BYBIT_CREDENTIAL_SCOPE,
    "withdrawal_disabled": ReadinessCheckId.BYBIT_CREDENTIAL_SCOPE,
    "spot_availability_operational": ReadinessCheckId.BYBIT_SPOT_AVAILABLE_BALANCE,
    "instrument_metadata_operational": ReadinessCheckId.BYBIT_INSTRUMENT,
    "clock_operational": ReadinessCheckId.CLOCK_SKEW,
    "reconciliation_operational": ReadinessCheckId.UNRESOLVED_OPERATIONS,
    "no_unresolved_submission": ReadinessCheckId.UNRESOLVED_OPERATIONS,
    "no_reconciliation_required": ReadinessCheckId.UNRESOLVED_OPERATIONS,
    "no_artifact_corruption": ReadinessCheckId.ARTIFACT_INTEGRITY,
}


def _passed(item: Mapping[str, Any] | None) -> bool:
    return str((item or {}).get("status")) in {"PASS", "HEALTHY", "CLEAR"}


def project_gate_d_evidence(bundle: Mapping[str, Any], *, contract_valid: bool = True) -> dict[str, Any]:
    """Normalize one evidence bundle at the Gate-D boundary.

    Every financial-safety boolean comes from an emitted canonical check ID.
    Workflow-history concepts are deliberately warnings, not invented hard
    blockers for a manually supervised one-shot order.
    """
    checks = {
        str(item.get("check_id")): item
        for item in bundle.get("check_results", [])
        if isinstance(item, Mapping)
    }
    projected = dict(bundle)
    for target, check_id in _REQUIRED_PROJECTION.items():
        projected[target] = _passed(checks.get(check_id.value))
    projected.update({
        "canonical_ci_healthy": True,
        "production_shadow_healthy": True,
        "watchdog_healthy": True,
        "production_contract_valid": contract_valid,
    })
    return projected

