"""Single canonical interface for manual/project execution reconciliation."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from .config import load_strategy_config
from .errors import LedgerValidationError
from .ledger import append_execution_once, confirmed_executions, read_executions
from .models import PortfolioState
from .paths import CONFIG_PATH, LEDGER_PATH
from .portfolio import derive_portfolio


class LedgerGateway:
    """Canonical write/read boundary shared by every BTC DCA interface."""

    def __init__(
        self,
        ledger_path: Path = LEDGER_PATH,
        config_path: Path = CONFIG_PATH,
    ) -> None:
        self.ledger_path = ledger_path
        self.config_path = config_path

    @staticmethod
    def _manual_id(
        executed_at_utc: str,
        executed_usd: Decimal,
        reference_price_usdt: Decimal,
        btc_quantity: Decimal | None,
    ) -> str:
        identity = {
            "executed_at_utc": executed_at_utc,
            "executed_usd": str(executed_usd),
            "reference_price_usdt": str(reference_price_usdt),
            "btc_quantity": None if btc_quantity is None else str(btc_quantity),
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        return f"execution_manual_{digest}"

    def _append_semantically_once(self, payload: dict) -> bool:
        """Treat cross-interface repeats of the same economic event as idempotent."""
        if self.ledger_path.exists():
            for existing in read_executions(self.ledger_path):
                if existing.execution_id != payload["execution_id"]:
                    continue
                keys = (
                    "executed_at_utc",
                    "asset",
                    "quote_currency",
                    "executed_usd",
                    "reference_price_usdt",
                    "btc_quantity",
                    "status",
                    "supersedes_execution_id",
                )
                for key in keys:
                    if existing.payload.get(key) != payload.get(key):
                        raise LedgerValidationError(
                            f"execution identity already exists with conflicting economic evidence: {payload['execution_id']}"
                        )
                return False
        return append_execution_once(self.ledger_path, payload)

    def record_execution(
        self,
        *,
        executed_at_utc: str,
        executed_usd: Decimal,
        reference_price_usdt: Decimal,
        source: str,
        note: str,
        btc_quantity: Decimal | None = None,
        execution_id: str | None = None,
    ) -> tuple[str, bool]:
        execution_id = execution_id or self._manual_id(
            executed_at_utc, executed_usd, reference_price_usdt, btc_quantity
        )
        payload = {
            "schema_version": "1.0.0",
            "execution_id": execution_id,
            "executed_at_utc": executed_at_utc,
            "asset": "BTC",
            "quote_currency": "USDT",
            "executed_usd": float(executed_usd),
            "reference_price_usdt": float(reference_price_usdt),
            "btc_quantity": None if btc_quantity is None else float(btc_quantity),
            "status": "reconciled",
            "reconciliation": {
                "source": (
                    "Chat 03 — Portfolio & Budget Tracker"
                    if source == "Chat 03 — Portfolio & Budget Tracker"
                    else "Project conversation — user-confirmed execution"
                ),
                "note": note,
            },
        }
        return execution_id, self._append_semantically_once(payload)

    def correct_execution(
        self,
        *,
        supersedes_execution_id: str,
        executed_at_utc: str,
        executed_usd: Decimal,
        reference_price_usdt: Decimal,
        source: str,
        note: str,
        btc_quantity: Decimal | None = None,
        execution_id: str | None = None,
    ) -> tuple[str, bool]:
        current = {item.execution_id for item in confirmed_executions(read_executions(self.ledger_path))}
        if supersedes_execution_id not in current:
            raise ValueError(f"cannot correct inactive execution: {supersedes_execution_id}")
        execution_id = execution_id or (
            self._manual_id(executed_at_utc, executed_usd, reference_price_usdt, btc_quantity)
            + "_corr"
        )
        payload = {
            "schema_version": "1.2.0",
            "execution_id": execution_id,
            "executed_at_utc": executed_at_utc,
            "asset": "BTC",
            "quote_currency": "USDT",
            "executed_usd": float(executed_usd),
            "reference_price_usdt": float(reference_price_usdt),
            "btc_quantity": None if btc_quantity is None else float(btc_quantity),
            "status": "reconciled",
            "reconciliation": {"source": source, "note": note},
            "supersedes_execution_id": supersedes_execution_id,
        }
        return execution_id, append_execution_once(self.ledger_path, payload)

    def cancel_execution(
        self,
        *,
        supersedes_execution_id: str,
        cancelled_at_utc: str,
        source: str,
        note: str,
        execution_id: str | None = None,
    ) -> tuple[str, bool]:
        current = {item.execution_id for item in confirmed_executions(read_executions(self.ledger_path))}
        if supersedes_execution_id not in current:
            raise ValueError(f"cannot cancel inactive execution: {supersedes_execution_id}")
        if execution_id is None:
            raw = f"{supersedes_execution_id}|{cancelled_at_utc}|void"
            execution_id = "execution_void_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        payload = {
            "schema_version": "1.2.0",
            "execution_id": execution_id,
            "executed_at_utc": cancelled_at_utc,
            "asset": "BTC",
            "quote_currency": "USDT",
            "executed_usd": 0,
            "reference_price_usdt": None,
            "btc_quantity": None,
            "status": "voided",
            "reconciliation": {"source": source, "note": note},
            "supersedes_execution_id": supersedes_execution_id,
        }
        return execution_id, append_execution_once(self.ledger_path, payload)

    def get_portfolio_state(self, calendar_month: str) -> PortfolioState:
        config = load_strategy_config(self.config_path)
        executions = read_executions(self.ledger_path)
        return derive_portfolio(executions, calendar_month, config.monthly_cap_usd)
