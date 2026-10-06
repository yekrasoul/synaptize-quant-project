import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from btc_dca_bridge.errors import (
    AllSourcesUnavailableError,
    ContradictoryPriceDataError,
    DataStaleError,
    DataUnavailableError,
    InsufficientHistoryError,
    InvalidMarketIdentityError,
    InvalidResponseError,
    RateLimitedError,
    SourceMismatchError,
)
from btc_dca_bridge.market_data.provider import (
    FallbackMarketDataProvider,
    TradingViewSnapshotSource,
)
from btc_dca_bridge.market_data.tradingview import (
    TRADINGVIEW_EXTERNAL_SYMBOL,
    TRADINGVIEW_SOURCE,
    TradingViewBybitSpotAdapter,
)
from btc_dca_bridge.schemas import validate_artifact


NOW = datetime(2026, 10, 7, 12, 5, tzinfo=UTC)
LAST_OPEN = datetime(2026, 10, 7, 11, 0, tzinfo=UTC)


def metadata(**changes):
    value = {
        "pro_name": "BYBIT:BTCUSDT",
        "exchange": "Bybit",
        "listed_exchange": "BYBIT",
        "type": "spot",
        "typespecs": ["crypto"],
        "description": "Bitcoin / TetherUS",
    }
    value.update(changes)
    return value


def bars(count=180, *, last_open=LAST_OPEN):
    first = last_open - timedelta(hours=count - 1)
    result = []
    for index in range(count):
        opened = first + timedelta(hours=index)
        high = "70000" if index == 90 else "66000"
        result.append(
            {
                "i": index,
                "v": [
                    int(opened.timestamp()),
                    "64000",
                    high,
                    "63000",
                    "65000",
                    "2.5",
                ],
            }
        )
    return result


class StubTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def fetch_chart(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def adapter(response=None, *, clock=lambda: NOW):
    transport = StubTransport(
        response or {"resolved_symbol": metadata(), "bars": bars()}
    )
    return TradingViewBybitSpotAdapter(transport=transport, clock=clock), transport


class TradingViewIdentityAndHistoryTest(unittest.TestCase):
    def test_exact_bybit_btcusdt_spot_identity_is_accepted(self):
        value, transport = adapter()
        ticker, history = value.fetch_inputs()
        self.assertEqual(ticker.external_symbol, TRADINGVIEW_EXTERNAL_SYMBOL)
        self.assertEqual(ticker.source, TRADINGVIEW_SOURCE)
        self.assertEqual(len(history.candles), 168)
        self.assertEqual(
            transport.calls,
            [
                {
                    "external_symbol": "BYBIT:BTCUSDT",
                    "interval_minutes": 60,
                    "bar_count": 180,
                }
            ],
        )

    def test_perpetual_symbol_is_rejected_without_normalization(self):
        value, _ = adapter(
            {"resolved_symbol": metadata(pro_name="BYBIT:BTCUSDT.P"), "bars": bars()}
        )
        with self.assertRaises(InvalidMarketIdentityError):
            value.fetch_inputs()

    def test_another_exchange_is_rejected(self):
        value, _ = adapter(
            {
                "resolved_symbol": metadata(
                    pro_name="BINANCE:BTCUSDT",
                    exchange="BINANCE",
                    listed_exchange="BINANCE",
                ),
                "bars": bars(),
            }
        )
        with self.assertRaises(SourceMismatchError):
            value.fetch_inputs()

    def test_another_pair_is_rejected(self):
        value, _ = adapter(
            {"resolved_symbol": metadata(pro_name="BYBIT:ETHUSDT"), "bars": bars()}
        )
        with self.assertRaises(SourceMismatchError):
            value.fetch_inputs()

    def test_generic_unverified_symbol_is_rejected(self):
        value, _ = adapter(
            {"resolved_symbol": metadata(pro_name="BTCUSDT"), "bars": bars()}
        )
        with self.assertRaises(SourceMismatchError):
            value.fetch_inputs()

    def test_derivative_metadata_is_rejected(self):
        value, _ = adapter(
            {
                "resolved_symbol": metadata(description="BTC perpetual swap"),
                "bars": bars(),
            }
        )
        with self.assertRaises(InvalidMarketIdentityError):
            value.fetch_inputs()

    def test_insufficient_history_is_rejected(self):
        value, _ = adapter({"resolved_symbol": metadata(), "bars": bars(167)})
        with self.assertRaises(InsufficientHistoryError):
            value.fetch_inputs()

    def test_boundary_gap_is_rejected(self):
        gapped = bars()
        del gapped[-80]
        value, _ = adapter({"resolved_symbol": metadata(), "bars": gapped})
        with self.assertRaises(InsufficientHistoryError):
            value.fetch_inputs()

    def test_boundary_ambiguity_is_rejected(self):
        ambiguous = bars()
        ambiguous[-1]["v"][0] += 60
        value, _ = adapter({"resolved_symbol": metadata(), "bars": ambiguous})
        with self.assertRaises(InsufficientHistoryError):
            value.fetch_inputs()

    def test_contradictory_ohlc_is_rejected(self):
        contradictory = bars()
        contradictory[-1]["v"][2] = "64000"
        contradictory[-1]["v"][4] = "65000"
        value, _ = adapter({"resolved_symbol": metadata(), "bars": contradictory})
        with self.assertRaises(ContradictoryPriceDataError):
            value.fetch_inputs()

    def test_stale_completed_bar_is_rejected_by_snapshot_policy(self):
        value, _ = adapter(
            {
                "resolved_symbol": metadata(),
                "bars": bars(last_open=LAST_OPEN - timedelta(hours=2)),
            }
        )
        source = TradingViewSnapshotSource(value)
        with self.assertRaises(DataStaleError):
            source.get_market_snapshot(captured_at_utc=NOW)

    def test_schema_valid_snapshot_and_repeatability(self):
        first_adapter, _ = adapter()
        second_adapter, _ = adapter()
        first = TradingViewSnapshotSource(first_adapter).get_market_snapshot(
            captured_at_utc=NOW
        )
        second = TradingViewSnapshotSource(second_adapter).get_market_snapshot(
            captured_at_utc=NOW
        )
        self.assertEqual(first, second)
        self.assertEqual(first.source, "tradingview")
        self.assertEqual(first.external_symbol, "BYBIT:BTCUSDT")
        self.assertEqual(first.observation_resolution_seconds, 3600)
        self.assertFalse(first.trade_level_exact)
        self.assertEqual(first.rolling_7d_high_usdt, 70000)
        validate_artifact("market_snapshot", first.to_dict())

    def test_legacy_snapshot_metadata_remains_schema_valid(self):
        value, _ = adapter()
        payload = TradingViewSnapshotSource(value).get_market_snapshot(
            captured_at_utc=NOW
        ).to_dict()
        for field in (
            "source",
            "external_symbol",
            "primary_source",
            "primary_failure_category",
            "fallback_attempted",
        ):
            payload["market_data_metadata"].pop(field)
        validate_artifact("market_snapshot", payload)


class StubSource:
    def __init__(self, source, result):
        self.source = source
        self.result = result
        self.calls = 0

    def get_market_snapshot(self, *, captured_at_utc):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def tradingview_snapshot():
    value, _ = adapter()
    return TradingViewSnapshotSource(value).get_market_snapshot(captured_at_utc=NOW)


def bybit_snapshot():
    return replace(tradingview_snapshot(), source="bybit_api")


class AtomicFallbackProviderTest(unittest.TestCase):
    def test_primary_success_does_not_call_tradingview(self):
        primary = StubSource("bybit_api", bybit_snapshot())
        fallback = StubSource("tradingview", AssertionError("must not be called"))
        result = FallbackMarketDataProvider(primary, fallback).get_market_snapshot(
            captured_at_utc=NOW
        )
        self.assertEqual(result.source, "bybit_api")
        self.assertEqual((primary.calls, fallback.calls), (1, 0))
        self.assertFalse(result.fallback_attempted)

    def test_primary_unavailable_attempts_complete_tradingview_fallback(self):
        primary = StubSource("bybit_api", DataUnavailableError("blocked"))
        fallback = StubSource("tradingview", tradingview_snapshot())
        result = FallbackMarketDataProvider(primary, fallback).get_market_snapshot(
            captured_at_utc=NOW
        )
        self.assertEqual((primary.calls, fallback.calls), (1, 1))
        self.assertEqual(result.source, "tradingview")
        self.assertEqual(result.external_symbol, "BYBIT:BTCUSDT")
        self.assertTrue(result.fallback_attempted)
        self.assertEqual(result.primary_failure_category, "SOURCE_UNAVAILABLE")
        validate_artifact("market_snapshot", result.to_dict())

    def test_primary_ticker_or_history_failure_cannot_be_composed(self):
        for failure in (
            DataUnavailableError("ticker unavailable"),
            InsufficientHistoryError("history unavailable"),
        ):
            with self.subTest(failure=failure):
                result = FallbackMarketDataProvider(
                    StubSource("bybit_api", failure),
                    StubSource("tradingview", tradingview_snapshot()),
                ).get_market_snapshot(captured_at_utc=NOW)
                self.assertEqual(result.source, "tradingview")
                self.assertEqual(result.current_price_usdt, 65000)
                self.assertEqual(result.rolling_7d_high_usdt, 70000)

    def test_all_documented_availability_categories_trigger_fallback(self):
        failures = (
            DataUnavailableError("unavailable"),
            RateLimitedError("limited"),
            InvalidResponseError("malformed"),
            InsufficientHistoryError("incomplete"),
        )
        for failure in failures:
            with self.subTest(code=failure.code.value):
                result = FallbackMarketDataProvider(
                    StubSource("bybit_api", failure),
                    StubSource("tradingview", tradingview_snapshot()),
                ).get_market_snapshot(captured_at_utc=NOW)
                self.assertEqual(result.primary_failure_category, failure.code.value)
                self.assertTrue(result.fallback_attempted)

    def test_tradingview_price_without_history_fails_entire_path(self):
        incomplete_adapter, _ = adapter(
            {
                "resolved_symbol": metadata(),
                "price": "65000",
                "bars": bars(10),
            }
        )
        provider = FallbackMarketDataProvider(
            StubSource("bybit_api", DataUnavailableError("blocked")),
            TradingViewSnapshotSource(incomplete_adapter),
        )
        with self.assertRaises(AllSourcesUnavailableError) as raised:
            provider.get_market_snapshot(captured_at_utc=NOW)
        self.assertEqual(
            raised.exception.fallback_failure.code.value, "INSUFFICIENT_HISTORY"
        )

    def test_both_sources_unavailable_exposes_attempts(self):
        provider = FallbackMarketDataProvider(
            StubSource("bybit_api", DataUnavailableError("blocked")),
            StubSource("tradingview", InvalidResponseError("malformed")),
        )
        with self.assertRaises(AllSourcesUnavailableError) as raised:
            provider.get_market_snapshot(captured_at_utc=NOW)
        error = raised.exception
        self.assertEqual(error.primary_source, "bybit_api")
        self.assertEqual(error.primary_failure.code.value, "SOURCE_UNAVAILABLE")
        self.assertEqual(error.fallback_source, "tradingview")
        self.assertEqual(error.fallback_failure.code.value, "INVALID_RESPONSE")
        self.assertTrue(error.fallback_attempted)

    def test_hard_primary_failures_do_not_trigger_fallback(self):
        for failure in (
            InvalidMarketIdentityError("derivative"),
            SourceMismatchError("wrong pair"),
            DataStaleError("stale"),
            ContradictoryPriceDataError("contradiction"),
        ):
            fallback = StubSource("tradingview", tradingview_snapshot())
            with self.subTest(failure=failure), self.assertRaises(type(failure)):
                FallbackMarketDataProvider(
                    StubSource("bybit_api", failure), fallback
                ).get_market_snapshot(captured_at_utc=NOW)
            self.assertEqual(fallback.calls, 0)

    def test_source_order_and_no_alternate_path_are_enforced(self):
        with self.assertRaises(ValueError):
            FallbackMarketDataProvider(
                StubSource("binance", bybit_snapshot()),
                StubSource("tradingview", tradingview_snapshot()),
            )

    def test_mislabeled_complete_path_cannot_create_a_mixed_snapshot(self):
        primary = StubSource("bybit_api", tradingview_snapshot())
        fallback = StubSource("tradingview", tradingview_snapshot())
        with self.assertRaises(SourceMismatchError):
            FallbackMarketDataProvider(primary, fallback).get_market_snapshot(
                captured_at_utc=NOW
            )
        self.assertEqual(fallback.calls, 0)


if __name__ == "__main__":
    unittest.main()
