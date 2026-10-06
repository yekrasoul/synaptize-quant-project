"""Atomic complete-source market snapshot selection and fallback policy."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ..errors import (
    AllSourcesUnavailableError,
    ContradictoryPriceDataError,
    DataStaleError,
    DataUnavailableError,
    InsufficientHistoryError,
    InvalidMarketIdentityError,
    InvalidResponseError,
    InvalidWindowError,
    MarketDataError,
    RateLimitedError,
    SourceMismatchError,
)
from ..models import MarketSnapshot
from ..schemas import validate_artifact
from .bybit import BYBIT_SOURCE, HOURLY_INTERVAL_MINUTES, MINUTE_INTERVAL_MINUTES, BybitSpotAdapter
from .snapshot import DEFAULT_MAX_INPUT_AGE, ROLLING_WINDOW, build_market_snapshot
from .tradingview import TRADINGVIEW_SOURCE, TradingViewBybitSpotAdapter


TRADINGVIEW_MAX_OBSERVATION_AGE = timedelta(minutes=65)


class CompleteSnapshotSource(Protocol):
    source: str

    def get_market_snapshot(self, *, captured_at_utc: datetime | None) -> MarketSnapshot: ...


class BybitSnapshotSource:
    """Retrieve and validate every component of one direct Bybit snapshot."""

    source = BYBIT_SOURCE

    def __init__(self, adapter: BybitSpotAdapter, *, clock=None) -> None:
        self.adapter = adapter
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_market_snapshot(self, *, captured_at_utc: datetime | None = None) -> MarketSnapshot:
        ticker = self.adapter.fetch_ticker()
        end = ticker.observed_at_utc or ticker.retrieved_at_utc
        if end.second or end.microsecond:
            raise InvalidWindowError(
                "Bybit Spot minute data cannot prove a sub-minute window boundary"
            )
        start = end - ROLLING_WINDOW
        first_full_hour = start.replace(minute=0, second=0, microsecond=0)
        if first_full_hour < start:
            first_full_hour += timedelta(hours=1)
        end_full_hour = end.replace(minute=0, second=0, microsecond=0)
        history = self.adapter.fetch_candles(
            start_utc=first_full_hour,
            end_utc=end_full_hour,
            interval_minutes=HOURLY_INTERVAL_MINUTES,
        )
        start_boundary = None
        if start < first_full_hour:
            start_boundary = self.adapter.fetch_candles(
                start_utc=start,
                end_utc=first_full_hour,
                interval_minutes=MINUTE_INTERVAL_MINUTES,
            )
        end_boundary = None
        if end_full_hour < end:
            end_boundary = self.adapter.fetch_candles(
                start_utc=end_full_hour,
                end_utc=end,
                interval_minutes=MINUTE_INTERVAL_MINUTES,
            )
        return build_market_snapshot(
            ticker,
            history,
            start_boundary_history=start_boundary,
            end_boundary_history=end_boundary,
            captured_at_utc=captured_at_utc or self._clock(),
        )


class TradingViewSnapshotSource:
    """Build one complete TradingView snapshot from one exact-symbol response."""

    source = TRADINGVIEW_SOURCE

    def __init__(self, adapter: TradingViewBybitSpotAdapter, *, clock=None) -> None:
        self.adapter = adapter
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_market_snapshot(self, *, captured_at_utc: datetime | None = None) -> MarketSnapshot:
        ticker, history = self.adapter.fetch_inputs()
        return build_market_snapshot(
            ticker,
            history,
            captured_at_utc=captured_at_utc or self._clock(),
            max_input_age=DEFAULT_MAX_INPUT_AGE,
            max_observation_age=TRADINGVIEW_MAX_OBSERVATION_AGE,
        )


HARD_FAILURES = (
    InvalidMarketIdentityError,
    SourceMismatchError,
    DataStaleError,
    InvalidWindowError,
    ContradictoryPriceDataError,
)
FALLBACK_FAILURES = (
    DataUnavailableError,
    RateLimitedError,
    InvalidResponseError,
    InsufficientHistoryError,
)


class FallbackMarketDataProvider:
    """Select a snapshot atomically: Bybit direct, then exact TradingView Spot."""

    def __init__(
        self,
        primary: CompleteSnapshotSource,
        fallback: CompleteSnapshotSource,
        *,
        clock=None,
    ) -> None:
        if primary.source != BYBIT_SOURCE or fallback.source != TRADINGVIEW_SOURCE:
            raise ValueError("source order must be bybit_api then tradingview")
        self.primary = primary
        self.fallback = fallback
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_market_snapshot(
        self, *, captured_at_utc: datetime | None = None
    ) -> MarketSnapshot:
        captured = captured_at_utc
        try:
            snapshot = self.primary.get_market_snapshot(captured_at_utc=captured)
        except HARD_FAILURES:
            raise
        except FALLBACK_FAILURES as primary_failure:
            return self._fallback(captured, primary_failure)
        self._validate_selected(snapshot, expected_source=BYBIT_SOURCE)
        return self._with_provenance(snapshot, primary_failure=None)

    def _fallback(
        self, captured: datetime | None, primary_failure: MarketDataError
    ) -> MarketSnapshot:
        try:
            snapshot = self.fallback.get_market_snapshot(captured_at_utc=captured)
        except HARD_FAILURES:
            raise
        except FALLBACK_FAILURES as fallback_failure:
            raise AllSourcesUnavailableError(
                "Bybit direct and TradingView exact Bybit Spot paths are unavailable",
                primary_source=self.primary.source,
                primary_failure=primary_failure,
                fallback_source=self.fallback.source,
                fallback_failure=fallback_failure,
            ) from fallback_failure
        self._validate_selected(snapshot, expected_source=TRADINGVIEW_SOURCE)
        return self._with_provenance(snapshot, primary_failure=primary_failure)

    @staticmethod
    def _validate_selected(snapshot: MarketSnapshot, *, expected_source: str) -> None:
        if snapshot.source != expected_source:
            raise SourceMismatchError(
                f"complete source path {expected_source} returned {snapshot.source}"
            )
        if (
            snapshot.source_exchange != "Bybit"
            or snapshot.market_type != "spot"
            or snapshot.symbol != "BTCUSDT"
            or snapshot.external_symbol != "BYBIT:BTCUSDT"
        ):
            raise InvalidMarketIdentityError(
                f"complete source path {expected_source} returned contradictory identity"
            )
        if not (
            snapshot.same_source_price_and_high
            and snapshot.full_168h_coverage
            and snapshot.fresh
        ):
            raise SourceMismatchError(
                f"complete source path {expected_source} did not prove snapshot atomicity"
            )

    @staticmethod
    def _with_provenance(
        snapshot: MarketSnapshot, *, primary_failure: MarketDataError | None
    ) -> MarketSnapshot:
        updated = replace(
            snapshot,
            primary_source=BYBIT_SOURCE,
            primary_failure_category=(
                None if primary_failure is None else primary_failure.code.value
            ),
            fallback_attempted=primary_failure is not None,
        )
        validate_artifact("market_snapshot", updated.to_dict())
        return updated
