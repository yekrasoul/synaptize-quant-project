import json
import socket
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

from btc_dca_bridge.errors import (
    DataUnavailableError,
    InsufficientHistoryError,
    InvalidMarketIdentityError,
    InvalidResponseError,
    MarketDataErrorCode,
    RateLimitedError,
    SourceMismatchError,
)
from btc_dca_bridge.market_data.bybit import (
    BYBIT_SOURCE,
    KLINE_PATH,
    TICKER_PATH,
    BybitSpotAdapter,
)
from btc_dca_bridge.market_data.http import HttpResponse, PublicHttpTransport


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def http_response(payload=None, *, status=200, body=None):
    if body is None:
        body = json.dumps(payload).encode("utf-8")
    return HttpResponse(status, {}, body)


def ticker_payload(*, price="65000.25", symbol="BTCUSDT", category="spot"):
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": category,
            "list": [{"symbol": symbol, "lastPrice": price}],
        },
        "time": 1791288000123,
    }


def row(open_time, *, open_="100", high="110", low="90", close="105", volume="2.5"):
    return [
        str(int(open_time.timestamp() * 1000)),
        open_,
        high,
        low,
        close,
        volume,
        "250",
    ]


def kline_payload(rows, *, symbol="BTCUSDT", category="spot"):
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"category": category, "symbol": symbol, "list": rows},
        "time": 1791288000123,
    }


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BybitTickerTest(unittest.TestCase):
    def test_valid_spot_ticker_response_is_normalized(self):
        transport = QueueTransport([http_response(ticker_payload())])
        ticker = BybitSpotAdapter(transport=transport, clock=lambda: NOW).fetch_ticker()

        self.assertEqual(ticker.price, Decimal("65000.25"))
        self.assertEqual(ticker.source, BYBIT_SOURCE)
        self.assertEqual((ticker.exchange, ticker.market, ticker.symbol), ("Bybit", "spot", "BTCUSDT"))
        self.assertEqual(ticker.retrieved_at_utc, NOW)
        self.assertIsNone(ticker.observed_at_utc)
        self.assertEqual(
            ticker.exchange_response_time_utc,
            datetime(2026, 10, 6, 12, 0, 0, 123000, tzinfo=UTC),
        )

    def test_ticker_request_explicitly_uses_spot_and_btcusdt(self):
        transport = QueueTransport([http_response(ticker_payload())])
        BybitSpotAdapter(transport=transport, clock=lambda: NOW).fetch_ticker()
        self.assertEqual(
            transport.calls,
            [(TICKER_PATH, {"category": "spot", "symbol": "BTCUSDT"})],
        )

    def test_symbol_mismatch_fails_closed(self):
        adapter = BybitSpotAdapter(
            transport=QueueTransport([http_response(ticker_payload(symbol="ETHUSDT"))]),
            clock=lambda: NOW,
        )
        with self.assertRaises(SourceMismatchError) as raised:
            adapter.fetch_ticker()
        self.assertEqual(raised.exception.code, MarketDataErrorCode.SOURCE_MISMATCH)

    def test_wrong_market_category_fails_closed(self):
        adapter = BybitSpotAdapter(
            transport=QueueTransport([http_response(ticker_payload(category="linear"))]),
            clock=lambda: NOW,
        )
        with self.assertRaises(InvalidMarketIdentityError) as raised:
            adapter.fetch_ticker()
        self.assertEqual(
            raised.exception.code, MarketDataErrorCode.INVALID_MARKET_IDENTITY
        )

    def test_derivative_price_fields_are_rejected_even_if_category_claims_spot(self):
        payload = ticker_payload()
        payload["result"]["list"][0]["markPrice"] = "64999"
        adapter = BybitSpotAdapter(
            transport=QueueTransport([http_response(payload)]), clock=lambda: NOW
        )
        with self.assertRaises(InvalidMarketIdentityError):
            adapter.fetch_ticker()

    def test_malformed_and_empty_ticker_payloads_fail_closed(self):
        malformed = ticker_payload()
        malformed["result"]["list"] = "not-a-list"
        empty = ticker_payload()
        empty["result"]["list"] = []
        for payload in (malformed, empty):
            with self.subTest(payload=payload):
                adapter = BybitSpotAdapter(
                    transport=QueueTransport([http_response(payload)]),
                    clock=lambda: NOW,
                )
                with self.assertRaises(InvalidResponseError):
                    adapter.fetch_ticker()

    def test_non_positive_and_non_numeric_ticker_prices_fail_closed(self):
        for price in ("0", "-1", "NaN", "not-a-number"):
            with self.subTest(price=price):
                adapter = BybitSpotAdapter(
                    transport=QueueTransport([http_response(ticker_payload(price=price))]),
                    clock=lambda: NOW,
                )
                with self.assertRaises(InvalidResponseError):
                    adapter.fetch_ticker()

    def test_deterministic_ticker_normalization(self):
        response = http_response(ticker_payload())
        first = BybitSpotAdapter(
            transport=QueueTransport([response]), clock=lambda: NOW
        ).fetch_ticker()
        second = BybitSpotAdapter(
            transport=QueueTransport([response]), clock=lambda: NOW
        ).fetch_ticker()
        self.assertEqual(first, second)
        self.assertEqual(first.to_dict(), second.to_dict())


class BybitCandleTest(unittest.TestCase):
    def _adapter(self, payloads, **kwargs):
        return BybitSpotAdapter(
            transport=QueueTransport([http_response(payload) for payload in payloads]),
            clock=lambda: NOW,
            **kwargs,
        )

    def test_valid_spot_klines_are_utc_aware_and_normalized(self):
        rows = [row(START + timedelta(hours=offset)) for offset in (2, 1, 0)]
        history = self._adapter([kline_payload(rows)]).fetch_candles(
            start_utc=START, end_utc=NOW
        )

        self.assertEqual(len(history.candles), 3)
        self.assertTrue(history.complete)
        self.assertEqual(history.interval_minutes, 60)
        self.assertEqual(history.source, BYBIT_SOURCE)
        self.assertEqual(history.coverage_start_utc, START)
        self.assertEqual(history.coverage_end_utc, NOW)
        candle = history.candles[0]
        self.assertEqual(candle.open, Decimal("100"))
        self.assertEqual(candle.volume, Decimal("2.5"))
        self.assertEqual(candle.open_time_utc.utcoffset(), timedelta(0))
        self.assertIs(candle.open_time_utc.tzinfo, UTC)

    def test_kline_request_explicitly_uses_spot_btcusdt_and_hourly_interval(self):
        rows = [row(START + timedelta(hours=offset)) for offset in (2, 1, 0)]
        transport = QueueTransport([http_response(kline_payload(rows))])
        BybitSpotAdapter(transport=transport, clock=lambda: NOW).fetch_candles(
            start_utc=START, end_utc=NOW
        )
        path, params = transport.calls[0]
        self.assertEqual(path, KLINE_PATH)
        self.assertEqual(params["category"], "spot")
        self.assertEqual(params["symbol"], "BTCUSDT")
        self.assertEqual(params["interval"], "60")
        self.assertEqual(params["limit"], 200)
        self.assertEqual(params["start"], int(START.timestamp() * 1000))
        self.assertEqual(params["end"], int(NOW.timestamp() * 1000))

    def test_malformed_kline_payload_fails_closed(self):
        malformed_rows = [[str(int(START.timestamp() * 1000)), "1", "2"]]
        with self.assertRaises(InvalidResponseError):
            self._adapter([kline_payload(malformed_rows)]).fetch_candles(
                start_utc=START, end_utc=START + timedelta(hours=1)
            )

    def test_empty_kline_result_signals_insufficient_history(self):
        with self.assertRaises(InsufficientHistoryError) as raised:
            self._adapter([kline_payload([])]).fetch_candles(
                start_utc=START, end_utc=NOW
            )
        self.assertEqual(
            raised.exception.code, MarketDataErrorCode.INSUFFICIENT_HISTORY
        )

    def test_non_positive_ohlc_fails_closed(self):
        for field in ("open_", "high", "low", "close"):
            values = {field: "0"}
            with self.subTest(field=field), self.assertRaises(InvalidResponseError):
                self._adapter([kline_payload([row(START, **values)])]).fetch_candles(
                    start_utc=START, end_utc=START + timedelta(hours=1)
                )

    def test_malformed_or_unaligned_timestamp_fails_closed(self):
        malformed = row(START)
        malformed[0] = "not-a-timestamp"
        unaligned = row(START + timedelta(minutes=1))
        for bad_row in (malformed, unaligned):
            with self.subTest(row=bad_row), self.assertRaises(InvalidResponseError):
                self._adapter([kline_payload([bad_row])]).fetch_candles(
                    start_utc=START, end_utc=START + timedelta(hours=1)
                )

    def test_duplicate_candles_are_deduplicated(self):
        page_one = [row(START + timedelta(hours=2)), row(START + timedelta(hours=1))]
        page_two = [row(START + timedelta(hours=1)), row(START)]
        history = self._adapter(
            [kline_payload(page_one), kline_payload(page_two)], page_limit=2
        ).fetch_candles(start_utc=START, end_utc=NOW)
        self.assertEqual(len(history.candles), 3)
        self.assertEqual(history.duplicate_count, 1)

    def test_out_of_order_candles_are_sorted_oldest_first(self):
        rows = [row(START + timedelta(hours=1)), row(START), row(START + timedelta(hours=2))]
        history = self._adapter([kline_payload(rows)]).fetch_candles(
            start_utc=START, end_utc=NOW
        )
        self.assertEqual(
            [candle.open_time_utc for candle in history.candles],
            [START, START + timedelta(hours=1), START + timedelta(hours=2)],
        )

    def test_bounded_pagination_walks_backwards_until_start(self):
        page_one = [row(START + timedelta(hours=2)), row(START + timedelta(hours=1))]
        page_two = [row(START)]
        adapter = self._adapter(
            [kline_payload(page_one), kline_payload(page_two)], page_limit=2
        )
        history = adapter.fetch_candles(start_utc=START, end_utc=NOW)
        self.assertEqual(history.request_count, 2)
        self.assertEqual(len(history.candles), 3)
        first_end = int(NOW.timestamp() * 1000)
        second_end = int((START + timedelta(hours=1)).timestamp() * 1000) - 1
        self.assertEqual(adapter._transport.calls[0][1]["end"], first_end)
        self.assertEqual(adapter._transport.calls[1][1]["end"], second_end)

    def test_missing_middle_candle_signals_incomplete_history(self):
        rows = [row(START + timedelta(hours=2)), row(START)]
        with self.assertRaises(InsufficientHistoryError):
            self._adapter([kline_payload(rows)]).fetch_candles(
                start_utc=START, end_utc=NOW
            )

    def test_pagination_exhaustion_signals_incomplete_history(self):
        page = [row(START + timedelta(hours=2))]
        with self.assertRaises(InsufficientHistoryError):
            self._adapter([kline_payload(page)], page_limit=1, max_pages=1).fetch_candles(
                start_utc=START, end_utc=NOW
            )

    def test_wrong_kline_category_and_symbol_fail_closed(self):
        cases = (
            (kline_payload([row(START)], category="linear"), InvalidMarketIdentityError),
            (kline_payload([row(START)], symbol="ETHUSDT"), SourceMismatchError),
        )
        for payload, error_type in cases:
            with self.subTest(error=error_type), self.assertRaises(error_type):
                self._adapter([payload]).fetch_candles(
                    start_utc=START, end_utc=START + timedelta(hours=1)
                )

    def test_naive_range_boundaries_are_rejected(self):
        adapter = self._adapter([])
        with self.assertRaises(ValueError):
            adapter.fetch_candles(
                start_utc=START.replace(tzinfo=None), end_utc=NOW
            )

    def test_deterministic_candle_normalization(self):
        rows = [row(START + timedelta(hours=offset)) for offset in (2, 1, 0)]
        first = self._adapter([kline_payload(rows)]).fetch_candles(
            start_utc=START, end_utc=NOW
        )
        second = self._adapter([kline_payload(rows)]).fetch_candles(
            start_utc=START, end_utc=NOW
        )
        self.assertEqual(first, second)
        self.assertEqual(first.to_dict(), second.to_dict())


class BybitHttpPolicyTest(unittest.TestCase):
    def _transport(self, side_effect):
        transport = PublicHttpTransport(
            "https://api.bybit.com",
            connect_timeout_seconds=1.25,
            read_timeout_seconds=4.5,
            max_attempts=3,
            backoff_seconds=0.01,
            sleep=lambda _: None,
        )
        transport._request_once = Mock(side_effect=side_effect)
        return transport

    def test_http_403_is_distinguished_and_not_retried(self):
        transport = self._transport([http_response(status=403, body=b"forbidden")])
        with self.assertRaises(DataUnavailableError) as raised:
            BybitSpotAdapter(transport=transport, clock=lambda: NOW).fetch_ticker()
        self.assertEqual(raised.exception.status_code, 403)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(transport._request_once.call_count, 1)

    def test_http_429_is_retried_then_reported_as_rate_limited(self):
        response = http_response(status=429, body=b"rate limited")
        transport = self._transport([response, response, response])
        with self.assertRaises(RateLimitedError) as raised:
            BybitSpotAdapter(transport=transport, clock=lambda: NOW).fetch_ticker()
        self.assertEqual(raised.exception.code, MarketDataErrorCode.RATE_LIMITED)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(transport._request_once.call_count, 3)

    def test_timeout_is_bounded_and_maps_to_data_unavailable(self):
        transport = self._transport(
            [socket.timeout("slow"), socket.timeout("slow"), socket.timeout("slow")]
        )
        with self.assertRaises(DataUnavailableError) as raised:
            BybitSpotAdapter(transport=transport, clock=lambda: NOW).fetch_ticker()
        self.assertTrue(raised.exception.retryable)
        self.assertIn("3 attempts", str(raised.exception))
        self.assertEqual(transport._request_once.call_count, 3)

    def test_retryable_5xx_then_success(self):
        transport = self._transport(
            [http_response(status=503, body=b"unavailable"), http_response(ticker_payload())]
        )
        ticker = BybitSpotAdapter(transport=transport, clock=lambda: NOW).fetch_ticker()
        self.assertEqual(ticker.price, Decimal("65000.25"))
        self.assertEqual(transport._request_once.call_count, 2)

    def test_retry_exhaustion_preserves_transient_5xx_identity(self):
        response = http_response(status=503, body=b"unavailable")
        transport = self._transport([response, response, response])
        with self.assertRaises(DataUnavailableError) as raised:
            BybitSpotAdapter(transport=transport, clock=lambda: NOW).fetch_ticker()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(transport._request_once.call_count, 3)

    def test_malformed_json_and_logical_api_errors_are_typed(self):
        malformed_adapter = BybitSpotAdapter(
            transport=QueueTransport([http_response(body=b"not-json")]),
            clock=lambda: NOW,
        )
        with self.assertRaises(InvalidResponseError):
            malformed_adapter.fetch_ticker()

        logical = ticker_payload()
        logical.update({"retCode": 10001, "retMsg": "bad parameter"})
        logical_adapter = BybitSpotAdapter(
            transport=QueueTransport([http_response(logical)]), clock=lambda: NOW
        )
        with self.assertRaises(DataUnavailableError) as raised:
            logical_adapter.fetch_ticker()
        self.assertIn("logical API error", str(raised.exception))

    def test_no_alternate_exchange_or_endpoint_fallback_exists(self):
        transport = QueueTransport(
            [http_response(status=403, body=b"forbidden")]
        )
        with self.assertRaises(DataUnavailableError):
            BybitSpotAdapter(transport=transport, clock=lambda: NOW).fetch_ticker()
        self.assertEqual(len(transport.calls), 1)
        self.assertIn(transport.calls[0][0], {TICKER_PATH, KLINE_PATH})
        self.assertEqual(transport.calls[0][1]["category"], "spot")
        self.assertEqual(transport.calls[0][1]["symbol"], "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
