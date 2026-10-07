"""Immutable values passed between the offline core layers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class MarketSnapshot:
    schema_version: str
    snapshot_id: str
    captured_at_utc: str
    source_exchange: str
    market_type: str
    symbol: str
    current_price_usdt: Decimal
    rolling_7d_high_usdt: Decimal
    window_start_utc: str
    window_end_utc: str
    same_source_price_and_high: bool
    full_168h_coverage: bool
    fresh: bool
    observation_resolution_seconds: int = 3600
    trade_level_exact: bool = False
    source: str = "bybit_api"
    external_symbol: str = "BYBIT:BTCUSDT"
    primary_source: str = "bybit_api"
    primary_failure_category: str | None = None
    fallback_attempted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "captured_at_utc": self.captured_at_utc,
            "source_exchange": self.source_exchange,
            "market_type": self.market_type,
            "symbol": self.symbol,
            "current_price_usdt": float(self.current_price_usdt),
            "rolling_7d_high_usdt": float(self.rolling_7d_high_usdt),
            "window_start_utc": self.window_start_utc,
            "window_end_utc": self.window_end_utc,
            "validation": {
                "same_source_price_and_high": self.same_source_price_and_high,
                "full_168h_coverage": self.full_168h_coverage,
                "fresh": self.fresh,
            },
            "market_data_metadata": {
                "observation_resolution_seconds": self.observation_resolution_seconds,
                "trade_level_exact": self.trade_level_exact,
                "source": self.source,
                "external_symbol": self.external_symbol,
                "primary_source": self.primary_source,
                "primary_failure_category": self.primary_failure_category,
                "fallback_attempted": self.fallback_attempted,
            },
        }


@dataclass(frozen=True)
class StrategyDecision:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


@dataclass(frozen=True)
class Execution:
    payload: dict[str, Any]

    @property
    def execution_id(self) -> str:
        return self.payload["execution_id"]

    @property
    def executed_at_utc(self) -> str:
        return self.payload["executed_at_utc"]

    @property
    def executed_usd(self) -> Decimal:
        return Decimal(str(self.payload["executed_usd"]))

    @property
    def reference_price_usdt(self) -> Decimal | None:
        value = self.payload.get("reference_price_usdt")
        return None if value is None else Decimal(str(value))


@dataclass(frozen=True)
class PortfolioSummary:
    calendar_month: str
    monthly_cap_usd: Decimal
    total_confirmed_usd_deployed: Decimal
    monthly_confirmed_usd_deployed: Decimal
    remaining_monthly_budget_usd: Decimal
    confirmed_execution_count: int
    monthly_confirmed_execution_count: int
    reference_price_derived_nominal_btc: Decimal
    weighted_reference_acquisition_price_usdt: Decimal | None
    derived_from_execution_ids: tuple[str, ...]
    schema_version: str = "1.1.0"
    as_of_utc: str = "1970-01-01T00:00:00Z"

    def to_dict(self) -> dict[str, Any]:
        def number(value: Decimal) -> int | float:
            return int(value) if value == value.to_integral_value() else float(value)

        return {
            "schema_version": self.schema_version,
            "as_of_utc": self.as_of_utc,
            "calendar_month": self.calendar_month,
            # 1.0.0 field names remain present in 1.1.0 for tolerant consumers.
            "monthly_spent_usd": number(self.monthly_confirmed_usd_deployed),
            "monthly_remaining_usd": number(self.remaining_monthly_budget_usd),
            "executions_count": self.confirmed_execution_count,
            "monthly_cap_usd": number(self.monthly_cap_usd),
            "total_confirmed_usd_deployed": number(self.total_confirmed_usd_deployed),
            "monthly_confirmed_usd_deployed": number(self.monthly_confirmed_usd_deployed),
            "remaining_monthly_budget_usd": number(self.remaining_monthly_budget_usd),
            "confirmed_execution_count": self.confirmed_execution_count,
            "monthly_confirmed_execution_count": self.monthly_confirmed_execution_count,
            "reference_price_derived_nominal_btc": float(
                self.reference_price_derived_nominal_btc
            ),
            "weighted_reference_acquisition_price_usdt": (
                None
                if self.weighted_reference_acquisition_price_usdt is None
                else float(self.weighted_reference_acquisition_price_usdt)
            ),
            "reference_price_quantity_disclaimer": (
                "Nominal BTC is derived from USD/reference price and is not actual exchange fill quantity."
            ),
            "derived_from_execution_ids": list(self.derived_from_execution_ids),
        }


# Phase 3.1 contract name; the Phase 2 class name remains an alias-compatible API.
PortfolioState = PortfolioSummary
