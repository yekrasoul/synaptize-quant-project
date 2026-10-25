"""Deterministic plain-text Telegram delivery for shadow-run outcomes."""

from __future__ import annotations

import http.client
import json
import socket
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlencode

from ..errors import NotificationError, NotificationErrorCode


TELEGRAM_HOST = "api.telegram.org"
MAX_MESSAGE_LENGTH = 4096


@dataclass(frozen=True)
class TelegramResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    attempts: int


@dataclass(frozen=True)
class TelegramDelivery:
    message_id: int
    attempts: int


class TelegramPoster(Protocol):
    def post(self, token: str, fields: Mapping[str, str]) -> TelegramResponse: ...


class TelegramTransport:
    """HTTPS form POST with bounded retries and separate connect/read timeouts."""

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("Telegram timeouts must be positive")
        if not 1 <= max_attempts <= 5:
            raise ValueError("Telegram max_attempts must be within 1..5")
        if backoff_seconds < 0:
            raise ValueError("Telegram backoff_seconds cannot be negative")
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep

    def post(self, token: str, fields: Mapping[str, str]) -> TelegramResponse:
        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._post_once(token, fields, attempt)
            except (socket.timeout, TimeoutError) as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    raise NotificationError(
                        "Telegram request timed out after bounded retry",
                        code=NotificationErrorCode.NOTIFICATION_TIMEOUT,
                        retryable=True,
                    ) from exc
            except OSError as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    raise NotificationError(
                        "Telegram connection failed after bounded retry",
                        code=NotificationErrorCode.NOTIFICATION_FAILED,
                        retryable=True,
                    ) from exc
            else:
                if response.status != 429 and not 500 <= response.status <= 599:
                    return response
                if attempt == self.max_attempts:
                    return response
                retry_after = _retry_after(response)
                self._sleep(
                    retry_after
                    if retry_after is not None
                    else self.backoff_seconds * (2 ** (attempt - 1))
                )
                continue
            self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))
        raise AssertionError(f"unreachable Telegram transport state: {last_error!r}")

    def _post_once(
        self, token: str, fields: Mapping[str, str], attempt: int
    ) -> TelegramResponse:
        connection = http.client.HTTPSConnection(
            TELEGRAM_HOST, timeout=self.connect_timeout_seconds
        )
        body = urlencode(fields).encode("utf-8")
        try:
            connection.request(
                "POST",
                f"/bot{token}/sendMessage",
                body=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "btc-dca-bridge/phase4",
                },
            )
            if connection.sock is not None:
                connection.sock.settimeout(self.read_timeout_seconds)
            response = connection.getresponse()
            return TelegramResponse(
                status=response.status,
                headers={key.lower(): value for key, value in response.getheaders()},
                body=response.read(),
                attempts=attempt,
            )
        finally:
            connection.close()


class TelegramNotifier:
    """Send one plain-text message without exposing credentials in errors."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        transport: TelegramPoster | None = None,
    ) -> None:
        if not isinstance(bot_token, str) or not bot_token or any(
            character in bot_token for character in ("/", "\n", "\r", "\x00")
        ):
            raise ValueError("Telegram bot token is missing or malformed")
        if not isinstance(chat_id, str) or not chat_id.strip() or any(
            character in chat_id for character in ("\n", "\r", "\x00")
        ):
            raise ValueError("Telegram chat ID is missing or malformed")
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._transport = transport or TelegramTransport()

    def send(self, message: str) -> TelegramDelivery:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Telegram message must be non-empty")
        if len(message) > MAX_MESSAGE_LENGTH:
            raise ValueError("Telegram message exceeds the 4096-character limit")
        response = self._transport.post(
            self._bot_token, {"chat_id": self._chat_id, "text": message}
        )
        if response.status == 429:
            raise NotificationError(
                "Telegram rate limit persisted after bounded retry",
                code=NotificationErrorCode.NOTIFICATION_RATE_LIMITED,
                status_code=429,
                retryable=True,
            )
        if not 200 <= response.status < 300:
            raise NotificationError(
                "Telegram returned a non-success HTTP status",
                code=NotificationErrorCode.NOTIFICATION_HTTP_ERROR,
                status_code=response.status,
                retryable=response.status >= 500,
            )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotificationError(
                "Telegram response is not valid JSON",
                code=NotificationErrorCode.NOTIFICATION_INVALID_RESPONSE,
            ) from exc
        if not isinstance(payload, dict):
            raise NotificationError(
                "Telegram response root is not an object",
                code=NotificationErrorCode.NOTIFICATION_INVALID_RESPONSE,
            )
        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if payload.get("ok") is not True or isinstance(message_id, bool) or not isinstance(message_id, int):
            raise NotificationError(
                "Telegram response did not confirm message delivery",
                code=NotificationErrorCode.NOTIFICATION_INVALID_RESPONSE,
            )
        return TelegramDelivery(message_id=message_id, attempts=response.attempts)


def format_success_message(outcome: Mapping[str, Any]) -> str:
    """Format only canonical structured values; no calculations occur here."""
    shadow = _mapping(outcome.get("shadow_result"), "shadow_result")
    market = _mapping(shadow.get("market_snapshot"), "market_snapshot")
    sentiment = _mapping(shadow.get("sentiment_snapshot"), "sentiment_snapshot")
    portfolio = _mapping(shadow.get("portfolio_state"), "portfolio_state")
    decision = _mapping(shadow.get("decision"), "decision")
    lines = [
        "BTC DCA — SHADOW",
        "BTC/USDT · Bybit",
        f"Price: ${_display_number(market['current_price_usdt'])}",
        f"7D High: ${_display_number(market['rolling_7d_high_usdt'])}",
        f"Drawdown: {_display_number(decision['drawdown_percent'], fixed_places=2)}%",
        f"Fear & Greed: {sentiment['value']}",
        "",
        f"Base: ${_display_number(decision['base_allocation_usd'])} · Multiplier: x{_display_number(decision['sentiment_multiplier'])}",
        f"Allocation: ${_display_number(decision['final_purchase_usd'])}",
        "",
        f"Month: ${_display_number(decision['monthly_spent_before_usd'])} / ${_display_number(portfolio['monthly_cap_usd'])}",
        f"Remaining: ${_display_number(decision['remaining_budget_before_usd'])}",
        "",
        f"DECISION: BUY ${_display_number(decision['final_purchase_usd'])} BTC",
        "EXECUTION: NONE — SHADOW",
        "",
        f"run: {outcome['run_id']}",
    ]
    return "\n".join(lines)


def format_failure_message(
    outcome: Mapping[str, Any],
    *,
    stage: str | None = None,
    category: str | None = None,
) -> str:
    failure = outcome.get("failure")
    details = failure if isinstance(failure, Mapping) else {}
    selected_stage = stage or str(details.get("stage") or "UNKNOWN")
    selected_category = category or str(details.get("reason") or details.get("category") or "UNKNOWN_FAILURE")
    source = details.get("source")
    lines = [
        "BTC DCA — SHADOW FAILED",
        f"Stage: {selected_stage}",
        f"Reason: {selected_category}",
    ]
    if isinstance(source, str) and source:
        lines.append(f"Source: {source}")
    if details.get("status_code") is not None:
        lines.append(f"HTTP: {details['status_code']}")
    if details.get("retryable") is not None:
        lines.append(f"Retryable: {str(details['retryable']).lower()}")
    cause_message = details.get("cause_message")
    if isinstance(cause_message, str) and cause_message:
        lines.append(f"Cause: {cause_message}")
    lines.extend(
        (
            "",
            "EXECUTION: NONE",
            f"run: {outcome.get('run_id', 'unknown')}",
        )
    )
    return "\n".join(lines)


def _display_number(value: object, *, fixed_places: int | None = None) -> str:
    """Round only for display and always emit fixed-point decimal text."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("notification value must be numeric") from exc
    if not number.is_finite():
        raise ValueError("notification value must be finite")
    places = 2 if fixed_places is None else fixed_places
    quantum = Decimal("1") if places == 0 else Decimal("1") / (10 ** places)
    rendered = format(number.quantize(quantum, rounding=ROUND_HALF_UP), "f")
    if fixed_places is None and "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"structured outcome lacks {label}")
    return value


def _retry_after(response: TelegramResponse) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return min(max(value, 0.0), 60.0)
