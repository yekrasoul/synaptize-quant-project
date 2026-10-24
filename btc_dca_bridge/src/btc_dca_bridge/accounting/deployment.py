"""Canonical confirmed-deployment projection across immutable legacy and V2."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .service import LedgerEntry
from ..ledger import confirmed_executions, validate_calendar_month
from ..models import Execution


@dataclass(frozen=True)
class ConfirmedDeployment:
    execution_id: str
    occurred_at_utc: str
    quote_deployed: Decimal


def confirmed_monthly_spend(
    *,
    legacy: Iterable[Execution],
    v2_entries: Iterable[LedgerEntry],
    calendar_month: str,
) -> Decimal:
    """Project active legacy truth plus confirmed V2 entries; legacy identity wins."""
    validate_calendar_month(calendar_month)
    active_legacy = confirmed_executions(tuple(legacy))
    rows = {
        item.execution_id: ConfirmedDeployment(
            item.execution_id,
            item.executed_at_utc,
            item.executed_usd,
        )
        for item in active_legacy
    }
    for entry in v2_entries:
        if entry.reconciliation_outcome != "CONFIRMED":
            continue
        if entry.execution_id and entry.execution_id not in rows:
            rows[entry.execution_id] = ConfirmedDeployment(
                entry.execution_id,
                entry.occurred_at_utc,
                entry.quote_decrease,
            )
    return sum(
        (
            row.quote_deployed
            for row in rows.values()
            if row.occurred_at_utc[:7] == calendar_month
        ),
        Decimal("0"),
    )
