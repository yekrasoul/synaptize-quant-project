"""Idempotent V2 accounting persistence port; does not rewrite legacy ledger."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from ..accounting.service import LedgerEntry


class AccountingRepository(Protocol):
    def post_once(self, entry: LedgerEntry) -> tuple[LedgerEntry, bool]: ...


class JsonAccountingRepository:
    def __init__(self, root: Path) -> None:
        self.root = root; self.root.mkdir(parents=True, exist_ok=True)

    def post_once(self, entry: LedgerEntry) -> tuple[LedgerEntry, bool]:
        path = self.root / f"{entry.execution_id}.json"
        payload = {"execution_id": entry.execution_id, "quote_decrease": str(entry.quote_decrease), "base_increase": str(entry.base_increase), "fee_quantity": str(entry.fee_quantity)}
        image = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        try: fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            prior = json.loads(path.read_text(encoding="utf-8"))
            if prior != payload: raise ValueError("accounting identity conflicts with prior evidence")
            return entry, False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(image); handle.flush(); os.fsync(handle.fileno())
        return entry, True
