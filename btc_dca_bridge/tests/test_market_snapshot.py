import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btc_dca_bridge.errors import (
    DataStaleError,
    InsufficientHistoryError,
    InvalidResponseError,
    SourceMismatchError,
)
from btc_dca_bridge.market_data.models import Candle, CandleHistory, Ticker
from btc_dca_bridge.market_data.snapshot import build_market_snapshot
from btc_dca_bridge.schemas import validate_artifact


WINDOW_END = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
WINDOW_START = WINDOW_END - timedelta(hours=168)
CAPTURED_AT = WINDOW_END + timedelta(minutes=1)


def candle(
    open_time,
    *,
    high="65000",
    source="bybit_api",
    symbol="BTCUSDT",
    interval_minutes=60,
):
    return Candle(
        open_time_utc=open_time,
        open=Decimal("64000"),
        high=Decimal(high),
        low=Decimal("63000"),
        close=Decimal("64500"),
        volume=Decimal("2"),
        source=source,
        exchange="Bybit",
        market="spot",
        symbol=symbol,
        interval_minutes=interval_minutes,
    )


def complete_candles():
    return tuple(candle(WINDOW_START + timedelta(hours=offset)) for offset in range(168))


def ticker(
    *,
    price="64500",
    retrieved_at=WINDOW_END,
    observed_at=None,
    source="bybit_api",
):
    return Ticker(
        price=Decimal(price),
        retrieved_at_utc=retrieved_at,
        exchange_response_time_utc=retrieved_at,
        observed_at_utc=observed_at,
        source=source,
        exchange="Bybit",
        market="spot",
        symbol="BTCUSDT",
    )


def history(candles=None, **changes):
    values = {
        "candles": complete_candles() if candles is None else tuple(candles),
        "requested_start_utc": WINDOW_START,
        "requested_end_utc": WINDOW_END,
        "coverage_start_utc": WINDOW_START,
        "coverage_end_utc": WINDOW_END,
        "retrieved_at_utc": WINDOW_END + timedelta(seconds=30),
        "interval_minutes": 60,
        "source": "bybit_api",
        "exchange": "Bybit",
        "market": "spot",
        "symbol": "BTCUSDT",
        "request_count": 1,
        "duplicate_count": 0,
        "complete": True,
    }
    values.update(changes)
    return CandleHistory(**values)


def boundary_history(start, end, *, high="65000", missing=None, retrieved_at=None):
    candles = tuple(
        candle(
            start + timedelta(minutes=offset),
            high=high,
            interval_minutes=1,
        )
        for offset in range(int((end - start).total_seconds() // 60))
        if start + timedelta(minutes=offset) != missing
    )
    return CandleHistory(
        candles=candles,
        requested_start_utc=start,
        requested_end_utc=end,
        coverage_start_utc=start,
        coverage_end_utc=end,
        retrieved_at_utc=retrieved_at or end + timedelta(seconds=30),
        interval_minutes=1,
        source="bybit_api",
        exchange="Bybit",
        market="spot",
        symbol="BTCUSDT",
        request_count=1,
        duplicate_count=0,
        complete=True,
    )


class ExactRollingHighTest(unittest.TestCase):
    def build(self, ticker_value=None, history_value=None, **kwargs):
        return build_market_snapshot(
            ticker_value or ticker(),
            history_value or history(),
            captured_at_utc=kwargs.pop("captured_at_utc", CAPTURED_AT),
            **kwargs,
        )

    def test_builds_schema_valid_exact_168_hour_snapshot(self):
        candles = list(complete_candles())
        candles[80] = candle(candles[80].open_time_utc, high="70000.25")

        snapshot = self.build(history_value=history(candles))

        self.assertEqual(snapshot.rolling_7d_high_usdt, Decimal("70000.25"))
        self.assertEqual(snapshot.window_start_utc, "2026-09-29T12:00:00Z")
        self.assertEqual(snapshot.window_end_utc, "2026-10-06T12:00:00Z")
        self.assertTrue(snapshot.same_source_price_and_high)
        self.assertTrue(snapshot.full_168h_coverage)
        self.assertTrue(snapshot.fresh)
        validate_artifact("market_snapshot", snapshot.to_dict())

    def test_observation_exactly_at_window_start_is_included(self):
        candles = list(complete_candles())
        candles[0] = candle(WINDOW_START, high="71000")

        snapshot = self.build(history_value=history(candles))

        self.assertEqual(snapshot.rolling_7d_high_usdt, Decimal("71000"))

    def test_observations_immediately_before_window_start_cannot_influence_high(self):
        before = candle(WINDOW_START - timedelta(hours=1), high="999999")
        candles = (before, *complete_candles())

        snapshot = self.build(
            history_value=history(
                candles,
                requested_start_utc=before.open_time_utc,
                coverage_start_utc=before.open_time_utc,
            )
        )

        self.assertEqual(snapshot.rolling_7d_high_usdt, Decimal("65000"))

    def test_ticker_observation_exactly_at_window_end_is_included(self):
        snapshot = self.build(ticker_value=ticker(price="72000"))

        self.assertEqual(snapshot.rolling_7d_high_usdt, Decimal("72000"))

    def test_source_observation_time_takes_precedence_over_retrieval_time(self):
        snapshot = self.build(
            ticker_value=ticker(
                price="72000",
                observed_at=WINDOW_END,
                retrieved_at=WINDOW_END + timedelta(seconds=30),
            )
        )

        self.assertEqual(snapshot.window_end_utc, "2026-10-06T12:00:00Z")

    def test_observations_immediately_after_window_end_cannot_influence_high(self):
        after = candle(WINDOW_END, high="999999")
        candles = (*complete_candles(), after)

        snapshot = self.build(
            history_value=history(
                candles,
                requested_end_utc=WINDOW_END + timedelta(hours=1),
                coverage_end_utc=WINDOW_END + timedelta(hours=1),
            )
        )

        self.assertEqual(snapshot.rolling_7d_high_usdt, Decimal("65000"))

    def test_non_hour_aligned_observation_time_fails_closed(self):
        non_aligned = WINDOW_END + timedelta(minutes=17, seconds=1)
        with self.assertRaisesRegex(
            InsufficientHistoryError, "sub-minute boundary coverage"
        ):
            self.build(
                ticker_value=ticker(retrieved_at=non_aligned),
                captured_at_utc=non_aligned + timedelta(seconds=30),
            )

    def test_missing_or_out_of_order_hour_fails_closed(self):
        complete = list(complete_candles())
        cases = (complete[:42] + complete[43:], list(reversed(complete)))
        for candles in cases:
            with self.subTest(count=len(candles)), self.assertRaises(
                InsufficientHistoryError
            ):
                self.build(history_value=history(candles))

    def test_partial_or_unproven_coverage_fails_closed(self):
        cases = (
            history(complete=False),
            history(coverage_start_utc=WINDOW_START + timedelta(hours=1)),
            history(coverage_end_utc=WINDOW_END - timedelta(hours=1)),
        )
        for value in cases:
            with self.subTest(history=value), self.assertRaises(
                InsufficientHistoryError
            ):
                self.build(history_value=value)

    def test_mixed_source_fails_closed_even_outside_window(self):
        foreign = candle(
            WINDOW_START - timedelta(hours=1),
            high="999999",
            source="other_api",
        )
        mixed = history(
            (foreign, *complete_candles()),
            requested_start_utc=foreign.open_time_utc,
            coverage_start_utc=foreign.open_time_utc,
        )

        with self.assertRaises(SourceMismatchError):
            self.build(history_value=mixed)

    def test_stale_ticker_or_history_fails_closed(self):
        old_ticker = ticker(retrieved_at=WINDOW_END - timedelta(hours=1))
        old_history = history(retrieved_at_utc=WINDOW_END - timedelta(minutes=6))
        for ticker_value, history_value in (
            (old_ticker, history()),
            (ticker(), old_history),
        ):
            with self.subTest(ticker=ticker_value), self.assertRaises(DataStaleError):
                build_market_snapshot(
                    ticker_value,
                    history_value,
                    captured_at_utc=WINDOW_END,
                    max_input_age=timedelta(minutes=5),
                )

    def test_contradictory_candle_fails_closed(self):
        bad = replace(complete_candles()[0], close=Decimal("70000"))
        candles = (bad, *complete_candles()[1:])
        with self.assertRaises(InvalidResponseError):
            self.build(history_value=history(candles))

    def test_snapshot_id_is_deterministic_for_identical_inputs(self):
        first = self.build()
        second = self.build()
        self.assertEqual(first, second)

    def test_minute_aligned_partial_boundaries_compose_exact_window(self):
        end = WINDOW_END + timedelta(minutes=17)
        start = end - timedelta(hours=168)
        captured = end + timedelta(minutes=1)
        hourly = tuple(
            candle(start.replace(minute=0) + timedelta(hours=offset))
            for offset in range(169)
        )
        start_end = start.replace(minute=0) + timedelta(hours=1)
        end_start = end.replace(minute=0)
        snapshot = build_market_snapshot(
            ticker(retrieved_at=end),
            history(
                hourly,
                requested_start_utc=start.replace(minute=0),
                requested_end_utc=end,
                coverage_start_utc=start.replace(minute=0),
                coverage_end_utc=end,
                retrieved_at_utc=end + timedelta(seconds=30),
            ),
            start_boundary_history=boundary_history(start, start_end, high="71000", retrieved_at=end + timedelta(seconds=30)),
            end_boundary_history=boundary_history(end_start, end, high="72000", retrieved_at=end + timedelta(seconds=30)),
            captured_at_utc=captured,
        )
        self.assertEqual(snapshot.window_start_utc, "2026-09-29T12:17:00Z")
        self.assertEqual(snapshot.window_end_utc, "2026-10-06T12:17:00Z")
        self.assertEqual(snapshot.rolling_7d_high_usdt, Decimal("72000"))
        self.assertEqual(snapshot.observation_resolution_seconds, 60)
        self.assertFalse(snapshot.trade_level_exact)
        validate_artifact("market_snapshot", snapshot.to_dict())

    def test_boundary_high_and_enclosed_hour_high_are_included(self):
        end = WINDOW_END + timedelta(minutes=17)
        start = end - timedelta(hours=168)
        captured = end + timedelta(minutes=1)
        hourly = [candle(start.replace(minute=0) + timedelta(hours=offset)) for offset in range(169)]
        hourly[80] = candle(hourly[80].open_time_utc, high="73000")
        snapshot = build_market_snapshot(
            ticker(retrieved_at=end),
            history(hourly, requested_start_utc=start.replace(minute=0), requested_end_utc=end, coverage_start_utc=start.replace(minute=0), coverage_end_utc=end, retrieved_at_utc=end + timedelta(seconds=30)),
            start_boundary_history=boundary_history(start, start.replace(minute=0) + timedelta(hours=1), high="71000", retrieved_at=end + timedelta(seconds=30)),
            end_boundary_history=boundary_history(end.replace(minute=0), end, high="72000", retrieved_at=end + timedelta(seconds=30)),
            captured_at_utc=captured,
        )
        self.assertEqual(snapshot.rolling_7d_high_usdt, Decimal("73000"))

    def test_missing_or_gapped_boundary_minute_fails_closed(self):
        end = WINDOW_END + timedelta(minutes=17)
        start = end - timedelta(hours=168)
        captured = end + timedelta(minutes=1)
        hourly = tuple(candle(start.replace(minute=0) + timedelta(hours=offset)) for offset in range(169))
        base = history(hourly, requested_start_utc=start.replace(minute=0), requested_end_utc=end, coverage_start_utc=start.replace(minute=0), coverage_end_utc=end, retrieved_at_utc=end + timedelta(seconds=30))
        for boundary in (None, boundary_history(start, start.replace(minute=0) + timedelta(hours=1), missing=start + timedelta(minutes=3), retrieved_at=end + timedelta(seconds=30))):
            with self.subTest(boundary=boundary), self.assertRaises(InsufficientHistoryError):
                build_market_snapshot(ticker(retrieved_at=end), base, start_boundary_history=boundary, end_boundary_history=boundary_history(end.replace(minute=0), end, retrieved_at=end + timedelta(seconds=30)), captured_at_utc=captured)


if __name__ == "__main__":
    unittest.main()
