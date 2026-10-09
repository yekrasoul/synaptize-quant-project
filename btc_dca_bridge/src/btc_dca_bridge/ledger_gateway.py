"""Single canonical interface for manual/project execution reconciliation."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .config import load_strategy_config
from .errors import AmbiguousManualExecutionError, LedgerValidationError
from .ledger import confirmed_executions, read_executions
from .ledger_sync import FileVersionedLedgerStore, LedgerReconciliationService
from .models import PortfolioState
from .paths import LEDGER_PATH
from .portfolio import derive_portfolio


_SOURCE_MAP = {
    "chat 01": "chat_01", "chat 02": "chat_02", "chat 04": "chat_04",
    "chat 03 — portfolio & budget tracker": "chat_03", "chat 03": "chat_03",
    "this project chat": "project_chat", "project chat correction": "project_chat",
    "any btc dca project chat": "project_chat", "another project chat": "project_chat",
    "another chat": "project_chat", "project conversation": "project_chat",
    "codex cli": "codex_cli", "github connector": "github_connector", "automation": "automation",
}


def _canonical_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerValidationError("execution timestamp must be an ISO UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LedgerValidationError("execution timestamp must include an explicit timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _interface(source: str) -> str:
    normalized = " ".join(source.strip().lower().split())
    if normalized not in _SOURCE_MAP:
        raise LedgerValidationError("intake interface is not in the bounded provenance vocabulary")
    return _SOURCE_MAP[normalized]


def _economic_match(left: dict, right: dict, *, calendar_date: bool = False) -> bool:
    keys = ("asset", "quote_currency", "executed_usd", "reference_price_usdt", "btc_quantity")
    if any(left.get(key) != right.get(key) for key in keys):
        return False
    if calendar_date:
        return left["executed_at_utc"][:10] == right["executed_at_utc"][:10]
    return left["executed_at_utc"] == right["executed_at_utc"]


class LedgerGateway:
    """Shared ledger boundary; every write uses latest-read/validate/CAS semantics."""
    def __init__(self, ledger_path: Path = LEDGER_PATH, config_path: Path | None = None, *, max_retries: int = 3) -> None:
        self.ledger_path = Path(ledger_path)
        self.config_path = config_path
        self.service = LedgerReconciliationService(FileVersionedLedgerStore(self.ledger_path), max_retries=max_retries)

    @staticmethod
    def _manual_id(executed_at_utc: str, executed_usd: Decimal, reference_price_usdt: Decimal, btc_quantity: Decimal | None) -> str:
        identity = {"executed_at_utc": executed_at_utc, "executed_usd": str(executed_usd), "reference_price_usdt": str(reference_price_usdt), "btc_quantity": None if btc_quantity is None else str(btc_quantity)}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
        return f"execution_manual_{digest}"

    def _write(self, builder, identity: str):
        result = self.service.apply(builder)
        return result.payload.get("execution_id", identity), result.created

    def record_execution(self, *, executed_at_utc: str, executed_usd: Decimal, reference_price_usdt: Decimal, source: str, note: str, btc_quantity: Decimal | None = None, execution_id: str | None = None, explicit_distinct_execution: bool = False) -> tuple[str, bool]:
        timestamp = _canonical_time(executed_at_utc)
        interface = _interface(source)
        deterministic_id = self._manual_id(timestamp, executed_usd, reference_price_usdt, btc_quantity)
        if explicit_distinct_execution and (not execution_id or execution_id == deterministic_id):
            raise LedgerValidationError("explicit distinct execution requires a distinct caller-provided execution_id")
        chosen_id = execution_id or deterministic_id
        requested = {"execution_id": chosen_id, "executed_at_utc": timestamp, "asset": "BTC", "quote_currency": "USDT", "executed_usd": float(executed_usd), "reference_price_usdt": float(reference_price_usdt), "btc_quantity": None if btc_quantity is None else float(btc_quantity), "status": "reconciled"}

        def build(history, active):
            for existing in history:
                if existing.execution_id == chosen_id:
                    if not _economic_match(existing.payload, requested):
                        raise LedgerValidationError(f"execution identity already exists with conflicting economic evidence: {chosen_id}")
                    return existing.payload
            if not explicit_distinct_execution:
                for existing in active:
                    same_day = existing.executed_at_utc[:10] == timestamp[:10]
                    same_pair = existing.payload.get("asset") == "BTC" and existing.payload.get("quote_currency") == "USDT"
                    if same_day and same_pair:
                        if _economic_match(existing.payload, requested, calendar_date=True):
                            if existing.executed_at_utc != timestamp:
                                raise AmbiguousManualExecutionError("possible duplicate manual execution: same date and economics but timestamp differs; explicitly reconcile to existing or provide a distinct identity")
                            return existing.payload
                        raise LedgerValidationError("same-day manual execution conflicts with or may be distinct from existing evidence; explicit distinct identity and intent are required")
            return {"schema_version": "1.3.0", **requested, "reconciliation": {"source": "Project user-confirmed execution", "note": note, "intake_interface": interface}}
        return self._write(build, chosen_id)

    def correct_execution(self, *, supersedes_execution_id: str, executed_at_utc: str, executed_usd: Decimal, reference_price_usdt: Decimal, source: str, note: str, btc_quantity: Decimal | None = None, execution_id: str | None = None) -> tuple[str, bool]:
        timestamp = _canonical_time(executed_at_utc)
        interface = _interface(source)
        base = self._manual_id(timestamp, executed_usd, reference_price_usdt, btc_quantity)
        identity = execution_id or "execution_corr_" + hashlib.sha256(f"{supersedes_execution_id}|{base}|correct".encode()).hexdigest()[:24]
        row = {"schema_version": "1.3.0", "execution_id": identity, "executed_at_utc": timestamp, "asset": "BTC", "quote_currency": "USDT", "executed_usd": float(executed_usd), "reference_price_usdt": float(reference_price_usdt), "btc_quantity": None if btc_quantity is None else float(btc_quantity), "status": "reconciled", "reconciliation": {"source": "Project user-confirmed execution", "note": note, "intake_interface": interface}, "supersedes_execution_id": supersedes_execution_id}
        def build(history, active):
            for item in history:
                if item.execution_id == identity:
                    if item.payload != row:
                        comparable = dict(item.payload); candidate = dict(row)
                        for payload in (comparable, candidate):
                            payload.get("reconciliation", {}).pop("note", None); payload.get("reconciliation", {}).pop("intake_interface", None)
                        if comparable != candidate: raise LedgerValidationError("correction identity conflicts with existing evidence")
                    return item.payload
            if supersedes_execution_id not in {item.execution_id for item in active}:
                raise LedgerValidationError(f"cannot correct inactive execution: {supersedes_execution_id}")
            return row
        return self._write(build, identity)

    def cancel_execution(self, *, supersedes_execution_id: str, cancelled_at_utc: str, source: str, note: str, execution_id: str | None = None) -> tuple[str, bool]:
        timestamp = _canonical_time(cancelled_at_utc); interface = _interface(source)
        identity = execution_id or "execution_void_" + hashlib.sha256(f"{supersedes_execution_id}|void".encode()).hexdigest()[:24]
        row = {"schema_version": "1.3.0", "execution_id": identity, "executed_at_utc": timestamp, "asset": "BTC", "quote_currency": "USDT", "executed_usd": 0, "reference_price_usdt": None, "btc_quantity": None, "status": "voided", "reconciliation": {"source": "Project user-confirmed execution", "note": note, "intake_interface": interface}, "supersedes_execution_id": supersedes_execution_id}
        def build(history, active):
            for item in history:
                if item.execution_id == identity or (item.payload.get("status") == "voided" and item.payload.get("supersedes_execution_id") == supersedes_execution_id): return item.payload
            if supersedes_execution_id not in {item.execution_id for item in active}: raise LedgerValidationError(f"cannot cancel inactive execution: {supersedes_execution_id}")
            return row
        return self._write(build, identity)

    def get_portfolio_state(self, calendar_month: str) -> PortfolioState:
        config = load_strategy_config(self.config_path) if self.config_path else load_strategy_config()
        executions = read_executions(self.ledger_path)
        return derive_portfolio(executions, calendar_month, config.monthly_cap_usd)
