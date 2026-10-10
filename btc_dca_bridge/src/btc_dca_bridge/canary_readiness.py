"""Read-only Phase 5.5D Bybit Spot canary readiness audit."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .artifacts import ArtifactStore
from .config import load_execution_config
from .final_readiness import final_readiness_audit
from .live_activation import read_activation_status
from .paths import DATA_PATH, LEDGER_PATH
from .production_enablement import read_enablement_status
from .real_money_authorization import read_authorization_status
from .readiness import ProductionReadinessService


EXPECTED_AUTHORIZATION = "NOT_AUTHORIZED"
EXPECTED_ACTIVATION = "NOT_ACTIVATED"
EXPECTED_ENABLEMENT = "NOT_ENABLED"


class BybitCanaryReadinessAudit:
    """Compose existing read-only readiness evidence without persistence."""

    def __init__(
        self,
        *,
        data_root: Path = DATA_PATH,
        ledger_path: Path = LEDGER_PATH,
        now: Callable[[], datetime] | None = None,
        client_factory: Callable[[], Any] | None = None,
        readiness_service_factory: Callable[..., Any] = ProductionReadinessService,
        local_audit_factory: Callable[..., Mapping[str, Any]] = final_readiness_audit,
        **readiness_probes: Any,
    ) -> None:
        self.data_root = Path(data_root)
        self.ledger_path = Path(ledger_path)
        self.now = now or (lambda: datetime.now(UTC))
        self.client_factory = client_factory
        self.readiness_service_factory = readiness_service_factory
        self.local_audit_factory = local_audit_factory
        self.readiness_probes = readiness_probes

    @staticmethod
    def _blocker(check_id: str, reason: str, remediation: str = "") -> dict[str, str]:
        return {"check_id": check_id, "reason": reason, "remediation": remediation}

    def evaluate(self) -> dict[str, Any]:
        service_kwargs: dict[str, Any] = {
            "data_root": self.data_root,
            "ledger_path": self.ledger_path,
            "now": self.now,
        }
        if self.client_factory is not None:
            service_kwargs["client_factory"] = self.client_factory
        service_kwargs.update(self.readiness_probes)
        readiness = self.readiness_service_factory(**service_kwargs).evaluate()
        checks = list(readiness.get("checks", []))
        blockers = list(readiness.get("blockers", []))

        # The final local audit supplies the Production Shadow state and the
        # canonical one-shot lifecycle states. It performs no network access.
        local = self.local_audit_factory(ledger_path=self.ledger_path, data_root=self.data_root, now_utc=self.now())
        local_checks = local.get("checks", {})
        shadow = local_checks.get("production_shadow", {"state": "UNKNOWN", "reason": "local shadow evidence unavailable"})
        checks.append({
            "check_id": "PRODUCTION_SHADOW",
            "category": "operations",
            "status": "PASS" if shadow.get("state") == "HEALTHY" else "UNAVAILABLE",
            "required": True,
            "evidence": shadow.get("reason", ""),
            "reason": shadow.get("reason", "Production Shadow is not proven healthy"),
        })
        if shadow.get("state") != "HEALTHY":
            blockers.append(self._blocker("PRODUCTION_SHADOW", str(shadow.get("reason", "Production Shadow is not healthy")), "Obtain a verified healthy scheduled Production Shadow run"))

        config = load_execution_config()
        config_safe = (
            config.live_execution_enabled is False
            and config.kill_switch is True
            and config.explicit_live_approval_required is True
            and config.monthly_cap_usd == Decimal("500")
            and config.exchange == "Bybit"
            and config.market_type == "spot"
            and config.symbol == "BTCUSDT"
            and config.order_submission == "not_implemented"
        )
        checks.append({
            "check_id": "EXECUTION_SAFETY",
            "category": "configuration",
            "status": "PASS" if config_safe else "FAIL",
            "required": True,
            "evidence": {
                "live_execution_enabled": config.live_execution_enabled,
                "kill_switch": config.kill_switch,
                "explicit_live_approval_required": config.explicit_live_approval_required,
                "monthly_cap_usd": str(config.monthly_cap_usd),
                "market": f"{config.exchange} {config.market_type} {config.symbol}",
                "order_submission": config.order_submission,
            },
            "reason": "checked-in execution configuration remains fail-closed" if config_safe else "execution configuration is not the required fail-closed policy",
        })
        if not config_safe:
            blockers.append(self._blocker("EXECUTION_SAFETY", "checked-in execution configuration is unsafe", "Restore the fail-closed execution defaults"))

        store = ArtifactStore(self.data_root)
        lifecycle = {
            "authorization": read_authorization_status(store=store).get("authorization", "UNKNOWN"),
            "activation": read_activation_status(store=store).get("activation", "UNKNOWN"),
            "production_enablement": read_enablement_status(store=store).get("enablement", "UNKNOWN"),
        }
        expected = {"authorization": EXPECTED_AUTHORIZATION, "activation": EXPECTED_ACTIVATION, "production_enablement": EXPECTED_ENABLEMENT}
        for name, value in lifecycle.items():
            check_id = name.upper()
            checks.append({"check_id": check_id, "category": "security", "status": "PASS" if value == expected[name] else "FAIL", "required": True, "evidence": value, "reason": f"current {name} state is {value}"})
            if value != expected[name]:
                blockers.append(self._blocker(check_id, f"current {name} state is {value}, expected {expected[name]}", "Keep one-shot execution lifecycle fail-closed"))

        # Duplicate the durable journal's stronger Phase 5.5C check when the
        # data root contains its attempt artifacts. This remains filesystem
        # read-only and is intentionally conservative.
        attempts = [
            path
            for root in (
                self.data_root / "order_submission_attempts",
                self.data_root / "live_execution_attempts",
            )
            for path in root.glob("*/*/*/*.json")
        ]
        if attempts:
            checks.append({"check_id": "SUBMISSION_ATTEMPTS", "category": "operations", "status": "FAIL", "required": True, "evidence": f"{len(attempts)} attempt artifact(s)", "reason": "prior submission evidence requires reconciliation"})
            blockers.append(self._blocker("SUBMISSION_ATTEMPTS", "prior submission attempt evidence exists", "Reconcile every prior identity before a canary"))

        dedup: dict[tuple[str, str], dict[str, str]] = {}
        for blocker in blockers:
            dedup[(str(blocker.get("check_id")), str(blocker.get("reason")))] = blocker
        blockers = list(dedup.values())
        ready = not blockers
        return {
            "readiness": "CANARY_READY_READ_ONLY" if ready else "CANARY_NOT_READY",
            "checks": checks,
            "blockers": blockers,
            "authenticated_reads_only": True,
            "order_submission_attempted": False,
            "execution": "BLOCKED",
            "authorization": lifecycle["authorization"],
            "activation": lifecycle["activation"],
            "production_enablement": lifecycle["production_enablement"],
            "live_execution_enabled": config.live_execution_enabled,
            "kill_switch": config.kill_switch,
            "order_submission": config.order_submission,
            "message": "NO ORDER EXECUTED",
        }


def bybit_canary_readiness(**kwargs: Any) -> dict[str, Any]:
    return BybitCanaryReadinessAudit(**kwargs).evaluate()


__all__ = ["BybitCanaryReadinessAudit", "bybit_canary_readiness"]
