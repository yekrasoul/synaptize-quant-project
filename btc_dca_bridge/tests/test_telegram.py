import json
import socket
import unittest

from btc_dca_bridge.errors import NotificationError, NotificationErrorCode
from btc_dca_bridge.notifications.telegram import (
    TelegramNotifier,
    TelegramResponse,
    TelegramTransport,
    format_failure_message,
    format_success_message,
)


def completed_outcome(*, source="bybit_api", fallback_attempted=False):
    return {
        "run_id": "run_20261007T110000Z_scheduled_123456789abc",
        "process_started_at_utc": "2026-10-07T11:04:19Z",
        "status": "completed",
        "shadow_result": {
            "market_snapshot": {
                "current_price_usdt": 80000,
                "rolling_7d_high_usdt": 100000,
                "market_data_metadata": {
                    "external_symbol": "BYBIT:BTCUSDT",
                    "fallback_attempted": fallback_attempted,
                    "primary_failure_category": (
                        "SOURCE_UNAVAILABLE" if fallback_attempted else None
                    ),
                    "primary_source": "bybit_api",
                    "source": source,
                },
            },
            "sentiment_snapshot": {"value": 35},
            "decision": {
                "drawdown_percent": -20,
                "base_allocation_usd": 75,
                "sentiment_multiplier": 1.3,
                "calculated_allocation_usd": 98,
                "monthly_spent_before_usd": 60,
                "remaining_budget_before_usd": 440,
                "final_purchase_usd": 98,
            },
        },
    }


class StaticTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, token, fields):
        self.calls.append((token, fields))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class SequenceTransport(TelegramTransport):
    def __init__(self, values, **kwargs):
        super().__init__(sleep=lambda _: None, **kwargs)
        self.values = list(values)
        self.calls = 0

    def _post_once(self, token, fields, attempt):
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return TelegramResponse(value, {}, b'{}', attempt)


class TelegramNotificationTest(unittest.TestCase):
    def test_success_message_contains_every_required_current_schema_field(self):
        message = format_success_message(
            completed_outcome(source="tradingview", fallback_attempted=True)
        )
        for expected in (
            "mode: SHADOW",
            "BTC price: $80000",
            "rolling 7D high: $100000",
            "drawdown: -20%",
            "Fear & Greed: 35",
            "base allocation: $75",
            "sentiment multiplier: x1.3",
            "calculated allocation: $98",
            "monthly spent: $60",
            "remaining monthly budget: $440",
            "FINAL PURCHASE: $98",
            "market source: tradingview",
            "NO ORDER EXECUTED",
            "SHADOW — BUY $98 BTC TODAY — NO ORDER EXECUTED",
        ):
            self.assertIn(expected, message)

    def test_tradingview_fallback_snapshot_formats_without_key_error(self):
        message = format_success_message(
            completed_outcome(source="tradingview", fallback_attempted=True)
        )
        self.assertIn("market source: tradingview", message)
        self.assertIn("SHADOW", message)
        self.assertIn("NO ORDER EXECUTED", message)
        self.assertIn("FINAL PURCHASE: $98", message)

    def test_bybit_direct_snapshot_formats_without_key_error(self):
        message = format_success_message(
            completed_outcome(source="bybit_api", fallback_attempted=False)
        )
        self.assertIn("market source: bybit_api", message)
        self.assertIn("SHADOW", message)
        self.assertIn("NO ORDER EXECUTED", message)
        self.assertIn("FINAL PURCHASE: $98", message)

    def test_failure_message_includes_available_diagnostics_and_no_order(self):
        outcome = {
            "run_id": "run_20261007T110000Z_scheduled_123456789abc",
            "process_started_at_utc": "2026-10-07T11:04:19Z",
            "failure": {
                "stage": "MARKET_DATA",
                "category": "SOURCE_UNAVAILABLE",
                "source": "bybit_api",
                "status_code": 403,
                "retryable": False,
                "cause_message": "SOURCE_UNAVAILABLE: Bybit returned HTTP 403 (forbidden, IP restriction, or IP rate limit)",
            },
        }
        message = format_failure_message(outcome)
        self.assertIn("PRODUCTION SHADOW FAILED", message)
        self.assertIn("failed stage: MARKET_DATA", message)
        self.assertIn("category: SOURCE_UNAVAILABLE", message)
        self.assertIn("source: bybit_api", message)
        self.assertIn("HTTP status: 403", message)
        self.assertIn("retryable: false", message)
        self.assertIn("cause: SOURCE_UNAVAILABLE: Bybit returned HTTP 403", message)
        self.assertIn("NO ORDER EXECUTED", message)
        self.assertNotIn("Traceback", message)

    def test_failure_message_omits_absent_diagnostics_and_untrusted_fields(self):
        outcome = {
            "run_id": "run-test",
            "process_started_at_utc": "2026-10-07T11:04:19Z",
            "failure": {
                "stage": "MARKET_DATA",
                "category": "SOURCE_UNAVAILABLE",
                "cause_message": "connection timed out",
                "secret": "bot-token-secret",
                "traceback": "Traceback (most recent call last)",
                "environment": "TELEGRAM_BOT_TOKEN=hidden",
                "raw_response_body": '{"retCode":10006}',
            },
        }
        message = format_failure_message(outcome)
        self.assertIn("cause: connection timed out", message)
        self.assertNotIn("bot-token-secret", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", message)
        self.assertNotIn("retCode", message)
        self.assertNotIn("HTTP status:", message)
        self.assertNotIn("retryable:", message)

    def test_success_message_substantive_v1_fields_remain_unchanged(self):
        message = format_success_message(completed_outcome())
        for expected in (
            "market source: bybit_api",
            "BTC price: $80000",
            "Fear & Greed: 35",
            "FINAL PURCHASE: $98",
            "SHADOW — BUY $98 BTC TODAY — NO ORDER EXECUTED",
        ):
            self.assertIn(expected, message)

    def test_success_delivery_uses_plain_text_and_confirms_message_id(self):
        response = TelegramResponse(
            200, {}, json.dumps({"ok": True, "result": {"message_id": 42}}).encode(), 1
        )
        transport = StaticTransport(response)
        notifier = TelegramNotifier("123:secret", "-100123", transport=transport)
        delivery = notifier.send("SHADOW — NO ORDER EXECUTED")
        self.assertEqual((delivery.message_id, delivery.attempts), (42, 1))
        self.assertEqual(
            transport.calls[0][1],
            {"chat_id": "-100123", "text": "SHADOW — NO ORDER EXECUTED"},
        )

    def test_timeout_is_bounded_and_token_never_enters_error(self):
        secret = "123:super-secret"
        transport = SequenceTransport(
            [socket.timeout("first"), socket.timeout("second")], max_attempts=2
        )
        with self.assertRaises(NotificationError) as raised:
            TelegramNotifier(secret, "chat-secret", transport=transport).send("test")
        self.assertEqual(transport.calls, 2)
        self.assertEqual(raised.exception.code, NotificationErrorCode.NOTIFICATION_TIMEOUT)
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn("chat-secret", str(raised.exception))

    def test_429_and_5xx_use_bounded_retry_policy(self):
        retry = SequenceTransport([500, 502, 200], max_attempts=3)
        response = retry.post("token", {"chat_id": "id", "text": "text"})
        self.assertEqual((response.status, response.attempts, retry.calls), (200, 3, 3))

        limited = StaticTransport(TelegramResponse(429, {}, b'{}', 3))
        with self.assertRaises(NotificationError) as raised:
            TelegramNotifier("token", "id", transport=limited).send("test")
        self.assertEqual(
            raised.exception.code, NotificationErrorCode.NOTIFICATION_RATE_LIMITED
        )
        self.assertTrue(raised.exception.retryable)

    def test_malformed_or_rejected_response_fails_closed(self):
        cases = (
            (TelegramResponse(200, {}, b"not-json", 1), NotificationErrorCode.NOTIFICATION_INVALID_RESPONSE),
            (TelegramResponse(200, {}, b"[]", 1), NotificationErrorCode.NOTIFICATION_INVALID_RESPONSE),
            (TelegramResponse(200, {}, b'{"ok":false}', 1), NotificationErrorCode.NOTIFICATION_INVALID_RESPONSE),
            (TelegramResponse(403, {}, b'{}', 1), NotificationErrorCode.NOTIFICATION_HTTP_ERROR),
        )
        for response, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(NotificationError) as raised:
                    TelegramNotifier("token", "id", transport=StaticTransport(response)).send("test")
                self.assertEqual(raised.exception.code, code)

    def test_notification_failure_is_distinct_from_completed_pipeline(self):
        outcome = completed_outcome()
        error = NotificationError(
            "delivery failed", code=NotificationErrorCode.NOTIFICATION_FAILED
        )
        with self.assertRaises(NotificationError):
            TelegramNotifier("token", "id", transport=StaticTransport(error)).send(
                format_success_message(outcome)
            )
        self.assertEqual(outcome["status"], "completed")


if __name__ == "__main__":
    unittest.main()
