"""Canonical confirmed-deployment projection across immutable legacy and V2."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping

from ..models import Execution
from ..ledger import validate_calendar_month


@dataclass(frozen=True)
class ConfirmedDeployment:
    execution_id: str
    occurred_at_utc: str
    quote_deployed: Decimal


def confirmed_monthly_spend(*, legacy: Iterable[Execution], v2_entries: Iterable[Mapping[str, object]], calendar_month: str) -> Decimal:
    """Legacy wins an identity collision; V2 rows require confirmed evidence."""
    validate_calendar_month(calendar_month)
    rows = {item.execution_id: ConfirmedDeployment(item.execution_id, item.executed_at_utc, item.executed_usd) for item in legacy if item.payload.get("status") == "reconciled"}
    for row in v2_entries:
        if row.get("reconciliation_outcome") != "CONFIRMED":
            continue
        identity = str(row.get("execution_id", ""))
        if identity and identity not in rows:
            rows[identity] = ConfirmedDeployment(identity, str(row["occurred_at_utc"]), Decimal(str(row["quote_decrease"])))
    return sum((row.quote_deployed for row in rows.values() if row.occurred_at_utc[:7] == calendar_month), Decimal("0"))
