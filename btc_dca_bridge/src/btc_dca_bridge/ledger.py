"""Read and validate the immutable canonical execution ledger."""

from __future__ import annotations

import json
import re
import hashlib
import os
import tempfile
import fcntl
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


def append_execution_once(path: Path, payload: dict) -> bool:
    """Durably append one fill under a process lock with recoverable identity claims."""
    validate_artifact("execution", payload)
    execution_id = payload["execution_id"]
    path.parent.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    claim_path = path.parent / f".{path.name}.{execution_id}.claim"
    lock_path = path.parent / f".{path.name}.lock"
    with lock_path.open("a+", encoding="ascii") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            claim = None
            if claim_path.exists():
                try:
                    claim = json.loads(claim_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise LedgerValidationError("execution identity claim is corrupt; manual recovery required") from exc
                if claim.get("payload_sha256") != digest:
                    raise LedgerValidationError("execution identity already claimed with conflicting evidence")
            else:
                fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"execution_id": execution_id, "payload_sha256": digest, "status": "prepared"}, handle, sort_keys=True)
                    handle.flush(); os.fsync(handle.fileno())
                _fsync_directory(path.parent)
            existing = read_executions(path) if path.exists() else ()
            for execution in existing:
                if execution.execution_id == execution_id:
                    if execution.payload != payload:
                        raise LedgerValidationError("execution identity already exists with different evidence")
                    _mark_claim_recorded(claim_path, execution_id, digest, path.parent)
                    return False
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(fd, canonical.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_directory(path.parent)
            _mark_claim_recorded(claim_path, execution_id, digest, path.parent)
            return True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _mark_claim_recorded(path: Path, execution_id: str, digest: str, directory: Path) -> None:
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.{execution_id}.", dir=directory)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"execution_id": execution_id, "payload_sha256": digest, "status": "recorded"}, handle, sort_keys=True)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
