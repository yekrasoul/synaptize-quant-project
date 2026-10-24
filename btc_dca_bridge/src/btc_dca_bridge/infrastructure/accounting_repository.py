"""Idempotent V2 accounting persistence port; does not rewrite legacy ledger."""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from ..accounting.service import LedgerEntry


class AccountingRepository(Protocol):
    def post_once(self, entry: LedgerEntry) -> tuple[LedgerEntry, bool]: ...
    def read_confirmed(self) -> tuple[LedgerEntry, ...]: ...


class JsonAccountingRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _payload(entry: LedgerEntry) -> dict[str, str]:
        if entry.reconciliation_outcome != "CONFIRMED" or not entry.occurred_at_utc:
            raise ValueError("accounting persistence requires confirmed timestamped evidence")
        return {
            "execution_id": entry.execution_id,
            "occurred_at_utc": entry.occurred_at_utc,
            "quote_decrease": str(entry.quote_decrease),
            "base_increase": str(entry.base_increase),
            "fee_quantity": str(entry.fee_quantity),
            "reconciliation_outcome": entry.reconciliation_outcome,
        }

    def post_once(self, entry: LedgerEntry) -> tuple[LedgerEntry, bool]:
        path = self.root / f"{entry.execution_id}.json"
        payload = self._payload(entry)
        image = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            prior = json.loads(path.read_text(encoding="utf-8"))
            if prior != payload:
                raise ValueError("accounting identity conflicts with prior evidence")
            return entry, False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(image)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return entry, True

    def read_confirmed(self) -> tuple[LedgerEntry, ...]:
        entries: list[LedgerEntry] = []
        for path in sorted(self.root.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("reconciliation_outcome") != "CONFIRMED":
                continue
            entries.append(
                LedgerEntry(
                    execution_id=str(payload["execution_id"]),
                    quote_decrease=Decimal(str(payload["quote_decrease"])),
                    base_increase=Decimal(str(payload["base_increase"])),
                    fee_quantity=Decimal(str(payload["fee_quantity"])),
                    occurred_at_utc=str(payload["occurred_at_utc"]),
                    reconciliation_outcome="CONFIRMED",
                )
            )
        return tuple(entries)
