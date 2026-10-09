"""Derive portfolio facts from confirmed execution history."""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from .errors import LedgerValidationError
from .ledger import confirmed_executions, validate_calendar_month
from .models import Execution, PortfolioState


def derive_portfolio(
    executions: Iterable[Execution], calendar_month: str, monthly_cap_usd: Decimal
) -> PortfolioState:
    if (
        not isinstance(monthly_cap_usd, Decimal)
        or not monthly_cap_usd.is_finite()
        or monthly_cap_usd <= 0
    ):
        raise LedgerValidationError("monthly cap must be a positive finite Decimal")
    confirmed = confirmed_executions(tuple(executions))
    validate_calendar_month(calendar_month)
    monthly = tuple(
        execution
        for execution in confirmed
        if execution.executed_at_utc[:7] == calendar_month
    )
    total_spend = sum((item.executed_usd for item in confirmed), Decimal(0))
    monthly_spend = sum((item.executed_usd for item in monthly), Decimal(0))
    remaining = monthly_cap_usd - monthly_spend
    if remaining < 0:
        raise LedgerValidationError(
            f"confirmed spend {monthly_spend} exceeds monthly cap {monthly_cap_usd}"
        )

    nominal_btc = sum(
        (
            item.executed_usd / item.reference_price_usdt
            for item in confirmed
            if item.reference_price_usdt is not None
        ),
        Decimal(0),
    )
    reference_priced_usd = sum(
        (
            item.executed_usd
            for item in confirmed
            if item.reference_price_usdt is not None
        ),
        Decimal(0),
    )
    weighted_price = (
        None if nominal_btc == 0 else reference_priced_usd / nominal_btc
    )
    as_of_utc = max(
        (item.executed_at_utc for item in confirmed),
        default=f"{calendar_month}-01T00:00:00Z",
    )
    return PortfolioState(
        schema_version="1.1.0",
        as_of_utc=as_of_utc,
        calendar_month=calendar_month,
        monthly_cap_usd=monthly_cap_usd,
        total_confirmed_usd_deployed=total_spend,
        monthly_confirmed_usd_deployed=monthly_spend,
        remaining_monthly_budget_usd=remaining,
        confirmed_execution_count=len(confirmed),
        monthly_confirmed_execution_count=len(monthly),
        reference_price_derived_nominal_btc=nominal_btc,
        weighted_reference_acquisition_price_usdt=weighted_price,
        derived_from_execution_ids=tuple(item.execution_id for item in confirmed),
    )
