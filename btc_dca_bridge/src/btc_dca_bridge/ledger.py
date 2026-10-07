"""Read and validate the immutable canonical execution ledger."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import LedgerValidationError, SchemaValidationError
from .models import Execution
from .paths import LEDGER_PATH
from .schemas import validate_artifact


_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def validate_calendar_month(calendar_month: str) -> None:
    if not isinstance(calendar_month, str) or not _MONTH.fullmatch(calendar_month):
        raise LedgerValidationError("calendar month must use YYYY-MM")


def read_executions(path: Path = LEDGER_PATH) -> tuple[Execution, ...]:
    executions: list[Execution] = []
    seen_ids: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LedgerValidationError(f"cannot read ledger {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerValidationError(
                f"malformed ledger JSON on line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(payload, dict):
            raise LedgerValidationError(f"ledger line {line_number} must be an object")
        try:
            validate_artifact("execution", payload)
        except SchemaValidationError as exc:
            raise LedgerValidationError(f"invalid execution on line {line_number}: {exc}") from exc
        execution_id = payload["execution_id"]
        if execution_id in seen_ids:
            raise LedgerValidationError(f"duplicate execution_id: {execution_id}")
        seen_ids.add(execution_id)
        executions.append(Execution(payload))
    return tuple(executions)


def confirmed_executions(
    executions: tuple[Execution, ...] | list[Execution],
) -> tuple[Execution, ...]:
    return tuple(execution for execution in executions if execution.payload["status"] == "reconciled")


def executions_for_month(
    executions: tuple[Execution, ...] | list[Execution], calendar_month: str
) -> tuple[Execution, ...]:
    validate_calendar_month(calendar_month)
    return tuple(
        execution
        for execution in confirmed_executions(executions)
        if execution.executed_at_utc[:7] == calendar_month
    )
