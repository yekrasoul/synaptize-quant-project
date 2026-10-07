import json
import unittest
from datetime import UTC, datetime, timedelta

from btc_dca_bridge.errors import (
    InvalidSentimentResponseError,
    InvalidSentimentTimestampError,
    InvalidSentimentValueError,
    SentimentDataStaleError,
    SentimentErrorCode,
    SentimentRateLimitedError,
    SentimentSourceUnavailableError,
)
from btc_dca_bridge.market_data.http import HttpResponse, TransportTimeout
from btc_dca_bridge.schemas import validate_artifact
from btc_dca_bridge.sentiment.alternative_me import (
    FEAR_GREED_PATH,
    AlternativeMeFearGreedAdapter,
)


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
OBSERVED = NOW - timedelta(hours=2)


def response(payload=None, *, status=200, body=None):
    return HttpResponse(status, {}, body if body is not None else json.dumps(payload).encode())


def payload(*, value="35", timestamp=None, classification="Fear"):
    return {
        "name": "Fear and Greed Index",
        "data": [{
            "value": value,
            "value_classification": classification,
            "timestamp": str(int((timestamp or OBSERVED).timestamp())),
        }],
        "metadata": {"error": None},
    }


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FearGreedAdapterTest(unittest.TestCase):
    def adapter(self, responses, **kwargs):
        return AlternativeMeFearGreedAdapter(
            transport=QueueTransport(responses), clock=lambda: NOW, **kwargs
        )

    def test_valid_minimum_maximum_and_band_boundaries_are_accepted(self):
        for value in (0, 100, 20, 21, 35, 36, 50, 51, 65, 66, 75, 76, 90, 91):
            with self.subTest(value=value):
                snapshot = self.adapter([response(payload(value=str(value)))]).fetch_current()
                self.assertEqual(snapshot.value, value)

    def test_request_is_single_source_current_reading(self):
        transport = QueueTransport([response(payload())])
        AlternativeMeFearGreedAdapter(transport=transport, clock=lambda: NOW).fetch_current()
        self.assertEqual(transport.calls, [(FEAR_GREED_PATH, {"limit": 1, "format": "json"})])

    def test_default_transport_has_an_explicit_bounded_retry_policy(self):
        adapter = AlternativeMeFearGreedAdapter(clock=lambda: NOW)
        self.assertEqual(adapter._transport.max_attempts, 3)
        self.assertGreater(adapter._transport.connect_timeout_seconds, 0)
        self.assertGreater(adapter._transport.read_timeout_seconds, 0)

    def test_invalid_values_fail_closed(self):
        for value in ("-1", "101", None, "fear", "35.5", True):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSentimentValueError):
                    self.adapter([response(payload(value=value))]).fetch_current()

    def test_malformed_or_ambiguous_payload_fails_closed(self):
        cases = [
            {},
            {"name": "Fear and Greed Index", "data": []},
            {"name": "Fear and Greed Index", "data": [payload()["data"][0], payload()["data"][0]]},
            {"name": "wrong", "data": [payload()["data"][0]]},
        ]
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(InvalidSentimentResponseError):
                    self.adapter([response(body)]).fetch_current()

    def test_freshness_policy_is_independent_and_configurable(self):
        fresh = self.adapter([response(payload(timestamp=NOW - timedelta(hours=35)))])
        self.assertTrue(fresh.fetch_current().fresh)
        stale = self.adapter([response(payload(timestamp=NOW - timedelta(hours=37)))])
        with self.assertRaises(SentimentDataStaleError) as raised:
            stale.fetch_current()
        self.assertEqual(raised.exception.code, SentimentErrorCode.DATA_STALE)

    def test_future_or_invalid_timestamp_is_rejected(self):
        with self.assertRaises(InvalidSentimentTimestampError):
            self.adapter([response(payload(timestamp=NOW + timedelta(seconds=1)))]).fetch_current()
        invalid = payload()
        invalid["data"][0]["timestamp"] = "tomorrow"
        with self.assertRaises(InvalidSentimentTimestampError):
            self.adapter([response(invalid)]).fetch_current()

    def test_normalization_is_deterministic_and_schema_valid(self):
        first = self.adapter([response(payload())]).fetch_current()
        second = self.adapter([response(payload())]).fetch_current()
        self.assertEqual(first, second)
        self.assertEqual(first.to_dict(), second.to_dict())
        validate_artifact("sentiment_snapshot", first.to_dict())

    def test_adapter_exposes_no_multiplier_or_purchase_amount(self):
        snapshot = self.adapter([response(payload())]).fetch_current()
        artifact = snapshot.to_dict()
        self.assertNotIn("multiplier", artifact)
        self.assertNotIn("purchase", " ".join(artifact))
        self.assertFalse(hasattr(snapshot, "multiplier"))

    def test_timeout_rate_limit_and_no_substitute_are_explicit(self):
        timeout = TransportTimeout("timed out", attempts=3)
        with self.assertRaises(SentimentSourceUnavailableError) as raised:
            self.adapter([timeout]).fetch_current()
        self.assertEqual(raised.exception.code, SentimentErrorCode.SOURCE_UNAVAILABLE)
        self.assertTrue(raised.exception.retryable)
        with self.assertRaises(SentimentRateLimitedError) as raised:
            self.adapter([response(status=429)]).fetch_current()
        self.assertEqual(raised.exception.code, SentimentErrorCode.RATE_LIMITED)

    def test_invalid_json_is_invalid_response(self):
        with self.assertRaises(InvalidSentimentResponseError):
            self.adapter([response(body=b"not json")]).fetch_current()

    def test_upstream_reported_error_is_not_treated_as_a_reading(self):
        invalid = payload()
        invalid["metadata"] = {"error": "maintenance"}
        with self.assertRaises(InvalidSentimentResponseError):
            self.adapter([response(invalid)]).fetch_current()


if __name__ == "__main__":
    unittest.main()
