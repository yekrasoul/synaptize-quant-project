"""Transport-neutral optimistic concurrency for the canonical append-only ledger."""
from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .errors import LedgerValidationError
from .ledger import confirmed_executions, parse_executions


@dataclass(frozen=True)
class LedgerSnapshot:
    content: bytes
    version: str


class VersionedLedgerStore(Protocol):
    def read(self) -> LedgerSnapshot: ...
    def compare_and_swap(self, *, expected_version: str, new_content: bytes) -> str: ...


class LedgerVersionConflict(LedgerValidationError):
    """The canonical ledger changed since the caller's snapshot."""


def _version(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _append_only(old: bytes, new: bytes) -> None:
    if not new.startswith(old):
        raise LedgerValidationError("versioned ledger update must preserve all existing history")


class InMemoryVersionedLedgerStore:
    def __init__(self, content: bytes = b"") -> None:
        self._content = content
        self._lock = threading.Lock()

    def read(self) -> LedgerSnapshot:
        with self._lock:
            content = self._content
            return LedgerSnapshot(content, _version(content))

    def compare_and_swap(self, *, expected_version: str, new_content: bytes) -> str:
        with self._lock:
            if _version(self._content) != expected_version:
                raise LedgerVersionConflict("canonical ledger changed; retry against latest version")
            _append_only(self._content, new_content)
            parse_executions(new_content)
            self._content = new_content
            return _version(new_content)


class FileVersionedLedgerStore:
    """Local CAS adapter; remote adapters should use a native immutable version token."""
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.cas.lock")

    def read(self) -> LedgerSnapshot:
        try:
            content = self.path.read_bytes()
        except FileNotFoundError:
            content = b""
        return LedgerSnapshot(content, _version(content))

    def compare_and_swap(self, *, expected_version: str, new_content: bytes) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current = self.read()
                if current.version != expected_version:
                    raise LedgerVersionConflict("canonical ledger changed; retry against latest version")
                _append_only(current.content, new_content)
                parse_executions(new_content)
                fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
                temp = Path(raw)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(new_content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, self.path)
                    dir_fd = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                finally:
                    temp.unlink(missing_ok=True)
                return _version(new_content)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class SynchronizedLedgerWrite:
    payload: dict
    created: bool
    version: str
    event_count: int
    active_execution_ids: tuple[str, ...]


class LedgerReconciliationService:
    def __init__(self, store: VersionedLedgerStore, *, max_retries: int = 3) -> None:
        if max_retries < 1 or max_retries > 5:
            raise ValueError("CAS retry bound must be between 1 and 5")
        self.store, self.max_retries = store, max_retries

    def apply(self, builder: Callable[[tuple, tuple], dict | None]) -> SynchronizedLedgerWrite:
        """Rebuild the same semantic operation against each fresh snapshot after CAS loss."""
        for _ in range(self.max_retries):
            snapshot = self.store.read()
            history = parse_executions(snapshot.content)
            active = confirmed_executions(history)
            payload = builder(history, active)
            if payload is None:
                return SynchronizedLedgerWrite({}, False, snapshot.version, len(history), tuple(x.execution_id for x in active))
            same_id = next((item for item in history if item.execution_id == payload.get("execution_id")), None)
            if same_id is not None:
                if same_id.payload != payload:
                    raise LedgerValidationError("execution identity already exists with conflicting evidence")
                return SynchronizedLedgerWrite(payload, False, snapshot.version, len(history), tuple(x.execution_id for x in active))
            line = __import__("json").dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            separator = b"" if not snapshot.content or snapshot.content.endswith(b"\n") else b"\n"
            new_content = snapshot.content + separator + line
            candidate = parse_executions(new_content)
            candidate_active = confirmed_executions(candidate)
            # Validate active projection and budget semantics before publication.
            from .config import load_strategy_config
            from .portfolio import derive_portfolio
            month = str(payload["executed_at_utc"])[:7]
            derive_portfolio(candidate, month, load_strategy_config().monthly_cap_usd)
            try:
                version = self.store.compare_and_swap(expected_version=snapshot.version, new_content=new_content)
                return SynchronizedLedgerWrite(payload, True, version, len(candidate), tuple(x.execution_id for x in candidate_active))
            except LedgerVersionConflict:
                continue
        raise LedgerVersionConflict("canonical ledger kept changing; no update was forced")
