"""Authenticated, read-only Bybit V5 Spot verification.

This module intentionally exposes only named GET operations.  It has no
generic private request method and no mutating HTTP verb.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlencode

from .execution import InstrumentRules, SubmissionState, parse_bybit_spot_instrument_info
from .market_data.http import HttpResponse

BYBIT_BASE_URL = "https://api.bybit.com"
API_KEY_ENV = "BYBIT_API_KEY"
API_SECRET_ENV = "BYBIT_API_SECRET"
DEFAULT_RECV_WINDOW = "5000"

USER_QUERY_API = "/v5/user/query-api"
ACCOUNT_INFO = "/v5/account/info"
WALLET_BALANCE = "/v5/account/wallet-balance"
ORDER_REALTIME = "/v5/order/realtime"
ORDER_HISTORY = "/v5/order/history"
EXECUTION_LIST = "/v5/execution/list"
INSTRUMENTS_INFO = "/v5/market/instruments-info"
PRIVATE_GET_ALLOWLIST = frozenset({USER_QUERY_API, ACCOUNT_INFO, WALLET_BALANCE, ORDER_REALTIME, ORDER_HISTORY, EXECUTION_LIST})
PUBLIC_GET_ALLOWLIST = frozenset({INSTRUMENTS_INFO})


class PrivateBybitError(ValueError):
    """Base error for private read verification."""


class AuthenticationError(PrivateBybitError): pass
class PermissionError(PrivateBybitError): pass
class PrivateTimeoutError(PrivateBybitError): pass
class PrivateRateLimitError(PrivateBybitError): pass
class MalformedBybitResponseError(PrivateBybitError): pass
class PrivateApiUnavailableError(PrivateBybitError): pass
class ContradictoryOrderEvidenceError(PrivateBybitError): pass


class CredentialClassification(str, Enum):
    READ_ONLY = "READ_ONLY"
    TRADE_CAPABLE = "TRADE_CAPABLE"
    UNSAFE_PERMISSION_SCOPE = "UNSAFE_PERMISSION_SCOPE"
    INVALID = "INVALID"


class OrderState(str, Enum):
    NOT_FOUND = "not_found"
    ACTIVE = "active"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ApiCredentialInfo:
    classification: CredentialClassification
    read_only: bool
    permissions: Mapping[str, tuple[str, ...]]
    identity: str | None = None
    expiry: str | None = None
    ip_restrictions: tuple[str, ...] = ()
    key_type: str | None = None


@dataclass(frozen=True)
class AccountInfo:
    unified_margin_status: str | None
    margin_mode: str | None
    spot_hedging_status: str | None
    updated_time: str | None


def _decimal(value: Any, label: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool): raise MalformedBybitResponseError(f"{label} is not numeric")
    try: parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc: raise MalformedBybitResponseError(f"{label} is not numeric") from exc
    if not parsed.is_finite() or (parsed < 0 if nonnegative else parsed <= 0):
        raise MalformedBybitResponseError(f"{label} is outside the allowed range")
    return parsed


@dataclass(frozen=True)
class WalletBalance:
    coin: str
    wallet_balance: Decimal
    locked: Decimal
    borrow_amount: Decimal
    accrued_interest: Decimal
    usd_value: Decimal
    # The current Unified wallet endpoint does not expose an authoritative
    # amount available for this exact Spot quote-buy operation.
    available_for_spot_quote_buy: Decimal | None = None

    @property
    def has_liability(self) -> bool: return self.borrow_amount > 0 or self.accrued_interest > 0


@dataclass(frozen=True)
class ReadOnlyOrder:
    order_link_id: str
    order_id: str | None
    state: OrderState
    raw_status: str
    executed_qty: Decimal
    executed_value: Decimal


@dataclass(frozen=True)
class ExecutionFill:
    exec_qty: Decimal
    exec_price: Decimal
    exec_value: Decimal
    exec_fee: Decimal
    exec_time: str
    exec_id: str
    order_id: str
    order_link_id: str
    category: str
    symbol: str


class ReadTransport(Protocol):
    def get(self, path: str, params: Mapping[str, str], headers: Mapping[str, str]) -> HttpResponse: ...


def _permission_token(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _classify_permissions(permissions: Mapping[str, tuple[str, ...]], read_only_value: Any) -> CredentialClassification:
    """Classify structured Bybit permission groups with unsafe precedence."""
    dangerous_groups = {"contracttrade", "derivatives", "derivativestrade", "position"}
    dangerous_actions = {"accounttransfer", "submembertransfer", "withdrawal", "withdraw", "borrow", "repay"}
    known_read = {"read", "spotread", "walletread", "accountread", "query"}
    known_spot_trade = {"spottrade", "orderentry", "spotorder"}
    has_trade = False
    has_read = False
    for raw_group, raw_actions in permissions.items():
        group = _permission_token(raw_group)
        actions = {_permission_token(action) for action in raw_actions}
        if group in dangerous_groups:
            return CredentialClassification.UNSAFE_PERMISSION_SCOPE
        if group == "wallet":
            if any(action in dangerous_actions or any(token in action for token in ("transfer", "withdraw", "borrow", "repay")) for action in actions):
                return CredentialClassification.UNSAFE_PERMISSION_SCOPE
            if not actions.issubset(known_read): return CredentialClassification.INVALID
            has_read = True
        elif group == "spot":
            if not actions or not actions.issubset(known_read | known_spot_trade): return CredentialClassification.INVALID
            has_trade |= bool(actions & known_spot_trade)
            has_read |= bool(actions & known_read)
        else:
            return CredentialClassification.INVALID
    is_read_only = read_only_value in (True, 1, "1")
    if is_read_only and has_trade: return CredentialClassification.INVALID
    if has_trade: return CredentialClassification.TRADE_CAPABLE
    if is_read_only and has_read: return CredentialClassification.READ_ONLY
    return CredentialClassification.INVALID


def canonical_query(params: Mapping[str, str | int]) -> str:
    return urlencode(sorted((str(key), str(value)) for key, value in params.items()))


def signature(timestamp: str, api_key: str, recv_window: str, query: str, api_secret: str) -> str:
    payload = f"{timestamp}{api_key}{recv_window}{query}"
    return hmac.new(api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


class SignedBybitTransport:
    """HTTPS GET transport with bounded retries and no mutating verb support."""
    def __init__(self, api_key: str, api_secret: str, *, base_url: str = BYBIT_BASE_URL,
                 clock: Callable[[], int] | None = None, timeout_seconds: float = 10.0,
                 max_attempts: int = 3, sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key or not api_secret: raise AuthenticationError("Bybit credentials are unavailable")
        if not base_url.startswith("https://"): raise ValueError("Bybit base URL must be HTTPS")
        from urllib.parse import urlsplit
        parsed = urlsplit(base_url)
        self._host, self._port, self._base_path = parsed.hostname, parsed.port, parsed.path.rstrip("/")
        self.api_key, self._api_secret = api_key, api_secret
        self._clock, self.timeout_seconds, self.max_attempts, self._sleep = clock or (lambda: int(time.time() * 1000)), timeout_seconds, max_attempts, sleep

    def get(self, path: str, params: Mapping[str, str], headers: Mapping[str, str]) -> HttpResponse:
        if path not in PRIVATE_GET_ALLOWLIST | PUBLIC_GET_ALLOWLIST: raise PermissionError("endpoint is not in the read-only allowlist")
        query = canonical_query(params)
        signed = dict(headers)
        timestamp = str(self._clock())
        signed.update({"X-BAPI-API-KEY": self.api_key, "X-BAPI-SIGN-TYPE": "2", "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": DEFAULT_RECV_WINDOW, "X-BAPI-SIGN": signature(timestamp, self.api_key, DEFAULT_RECV_WINDOW, query, self._api_secret)})
        import http.client
        target = f"{self._base_path}{path}" + (f"?{query}" if query else "")
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            conn = http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout_seconds)
            try:
                conn.request("GET", target, headers=signed)
                raw = conn.getresponse()
                response = HttpResponse(raw.status, {k.lower(): v for k, v in raw.getheaders()}, raw.read())
                if response.status not in {429} and response.status < 500: return response
                if attempt == self.max_attempts: return response
            except (socket.timeout, TimeoutError) as exc:
                last = exc
                if attempt == self.max_attempts: raise PrivateTimeoutError("Bybit private read timed out") from exc
            except OSError as exc:
                last = exc
                if attempt == self.max_attempts: raise PrivateApiUnavailableError("Bybit private read unavailable") from exc
            finally: conn.close()
            self._sleep(0.2 * (2 ** (attempt - 1)))
        raise PrivateApiUnavailableError("Bybit private read unavailable") from last


class BybitPrivateReadClient:
    def __init__(self, transport: ReadTransport): self._transport = transport

    @classmethod
    def from_environment(cls, *, transport_factory: Callable[[str, str], ReadTransport] = SignedBybitTransport) -> "BybitPrivateReadClient":
        key, secret = os.environ.get(API_KEY_ENV), os.environ.get(API_SECRET_ENV)
        if not key or not secret: raise AuthenticationError(f"{API_KEY_ENV} and {API_SECRET_ENV} must be set")
        return cls(transport_factory(key, secret))

    def _read(self, endpoint: str, params: Mapping[str, str], *, public: bool = False) -> Mapping[str, Any]:
        if endpoint not in (PUBLIC_GET_ALLOWLIST if public else PRIVATE_GET_ALLOWLIST): raise PermissionError("endpoint is not in the read-only allowlist")
        response = self._transport.get(endpoint, params, {"Accept": "application/json"})
        if response.status == 401 or response.status == 403: raise AuthenticationError("Bybit authentication failed")
        if response.status == 429: raise PrivateRateLimitError("Bybit private read rate limited")
        if response.status >= 500: raise PrivateApiUnavailableError("Bybit private API unavailable")
        try: body = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise MalformedBybitResponseError("Bybit response is not JSON") from exc
        if not isinstance(body, dict): raise MalformedBybitResponseError("Bybit response root is malformed")
        code = body.get("retCode")
        if code != 0:
            if code in {10003, 10004}: raise AuthenticationError("Bybit authentication failed")
            if code == 10005: raise PermissionError("Bybit permission scope rejected the read")
            raise MalformedBybitResponseError("Bybit response error")
        result = body.get("result")
        if not isinstance(result, dict): raise MalformedBybitResponseError("Bybit result is malformed")
        return result

    def credential_info(self) -> ApiCredentialInfo:
        result = self._read(USER_QUERY_API, {})
        permissions_raw = result.get("permissions", result.get("permission"))
        if not isinstance(permissions_raw, dict): raise MalformedBybitResponseError("API permissions are missing")
        if any(not isinstance(value, list) for value in permissions_raw.values()): raise MalformedBybitResponseError("API permissions are malformed")
        permissions = {str(k): tuple(str(v) for v in value) for k, value in permissions_raw.items()}
        classification = _classify_permissions(permissions, result.get("readOnly"))
        ips = result.get("ips", [])
        if not isinstance(ips, list): raise MalformedBybitResponseError("API IP restrictions are malformed")
        return ApiCredentialInfo(classification, result.get("readOnly") in (True, 1, "1"), permissions, result.get("id") or result.get("apiKeyId"), result.get("deadlineDate") or result.get("expiry"), tuple(str(v) for v in ips), result.get("type"))

    def account_info(self) -> AccountInfo:
        r = self._read(ACCOUNT_INFO, {})
        if any(field not in r for field in ("unifiedMarginStatus", "marginMode", "spotHedgingStatus", "updatedTime")):
            raise MalformedBybitResponseError("account info is incomplete")
        return AccountInfo(r.get("unifiedMarginStatus"), r.get("marginMode"), r.get("spotHedgingStatus"), r.get("updatedTime"))

    def wallet_balances(self) -> tuple[WalletBalance, ...]:
        r = self._read(WALLET_BALANCE, {"accountType": "UNIFIED"})
        accounts = r.get("list")
        if not isinstance(accounts, list) or len(accounts) != 1 or not isinstance(accounts[0].get("coin"), list): raise MalformedBybitResponseError("wallet balance is malformed")
        values = []
        for row in accounts[0]["coin"]:
            if row.get("coin") not in {"BTC", "USDT"}: continue
            try:
                values.append(WalletBalance(row["coin"], _decimal(row.get("walletBalance"), "walletBalance", nonnegative=True), _decimal(row.get("locked", "0"), "locked", nonnegative=True), _decimal(row.get("borrowAmount", "0"), "borrowAmount", nonnegative=True), _decimal(row.get("accruedInterest", "0"), "accruedInterest", nonnegative=True), _decimal(row.get("usdValue", "0"), "usdValue", nonnegative=True)))
            except KeyError as exc: raise MalformedBybitResponseError("wallet balance is incomplete") from exc
        return tuple(values)

    def instrument_rules(self) -> InstrumentRules:
        result = self._read(INSTRUMENTS_INFO, {"category": "spot", "symbol": "BTCUSDT"}, public=True)
        return parse_bybit_spot_instrument_info({"retCode": 0, "result": result})

    @staticmethod
    def _order(row: Mapping[str, Any], requested_client_order_id: str) -> ReadOnlyOrder:
        if row.get("orderLinkId") != requested_client_order_id:
            raise MalformedBybitResponseError("order evidence orderLinkId does not match requested client_order_id")
        for field, expected in (("category", "spot"), ("symbol", "BTCUSDT")):
            if field not in row:
                return ReadOnlyOrder(str(row.get("orderLinkId", "")), row.get("orderId"), OrderState.AMBIGUOUS, "missing_identity", Decimal("0"), Decimal("0"))
            if row.get(field) != expected:
                raise MalformedBybitResponseError(f"order evidence {field} does not match approved Spot identity")
        status = str(row.get("orderStatus"))
        state = {"New": OrderState.ACTIVE, "Untriggered": OrderState.ACTIVE, "PartiallyFilled": OrderState.PARTIALLY_FILLED, "Filled": OrderState.FILLED, "Cancelled": OrderState.CANCELLED, "Rejected": OrderState.REJECTED}.get(status, OrderState.AMBIGUOUS)
        return ReadOnlyOrder(str(row.get("orderLinkId", "")), row.get("orderId"), state, status, _decimal(row.get("cumExecQty", "0"), "cumExecQty", nonnegative=True), _decimal(row.get("cumExecValue", "0"), "cumExecValue", nonnegative=True))

    def _orders(self, endpoint: str, client_order_id: str) -> tuple[ReadOnlyOrder, ...]:
        r = self._read(endpoint, {"category": "spot", "symbol": "BTCUSDT", "orderLinkId": client_order_id})
        rows = r.get("list")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows): raise MalformedBybitResponseError("order result is malformed")
        return tuple(self._order(row, client_order_id) for row in rows)

    def lookup_order(self, client_order_id: str) -> ReadOnlyOrder:
        rows = self._orders(ORDER_REALTIME, client_order_id) + self._orders(ORDER_HISTORY, client_order_id)
        if not rows: return ReadOnlyOrder(client_order_id, None, OrderState.NOT_FOUND, "", Decimal("0"), Decimal("0"))
        states = {row.state for row in rows}
        identities = {(row.order_id, row.state) for row in rows}
        if len(states) != 1 or len(identities) != 1: return ReadOnlyOrder(client_order_id, None, OrderState.AMBIGUOUS, "contradictory", Decimal("0"), Decimal("0"))
        return rows[0]

    def executions(self, client_order_id: str) -> tuple[ExecutionFill, ...]:
        rows = self._read(EXECUTION_LIST, {"category": "spot", "symbol": "BTCUSDT", "orderLinkId": client_order_id}).get("list")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows): raise MalformedBybitResponseError("execution result is malformed")
        try:
            for row in rows:
                if row.get("orderLinkId") != client_order_id: raise MalformedBybitResponseError("execution evidence orderLinkId does not match requested client_order_id")
                for field, expected in (("category", "spot"), ("symbol", "BTCUSDT")):
                    if row.get(field) != expected: raise MalformedBybitResponseError(f"execution evidence {field} does not match approved Spot identity")
            return tuple(ExecutionFill(_decimal(row.get("execQty"), "execQty"), _decimal(row.get("execPrice"), "execPrice"), _decimal(row.get("execValue"), "execValue", nonnegative=True), _decimal(row.get("execFee", "0"), "execFee", nonnegative=True), str(row["execTime"]), str(row["execId"]), str(row["orderId"]), str(row["orderLinkId"]), str(row["category"]), str(row["symbol"])) for row in rows)
        except (KeyError, AttributeError, TypeError) as exc: raise MalformedBybitResponseError("execution fill is malformed") from exc

    def submission_state(self, client_order_id: str) -> str:
        try:
            order = self.lookup_order(client_order_id)
            fills = self.executions(client_order_id)
        except (PrivateBybitError, KeyError, TypeError, ValueError):
            return "ambiguous"
        fill_order_ids = {fill.order_id for fill in fills if fill.order_id}
        if order.order_id and fill_order_ids and (fill_order_ids != {order.order_id}): return "ambiguous"
        if fills or order.state is OrderState.FILLED: return "confirmed"
        if order.state in {OrderState.ACTIVE, OrderState.PARTIALLY_FILLED, OrderState.AMBIGUOUS}: return "ambiguous"
        if order.state in {OrderState.CANCELLED, OrderState.REJECTED, OrderState.NOT_FOUND}:
            return "conclusively_absent" if order.state is OrderState.NOT_FOUND else "ambiguous"
        return "ambiguous"


class BybitSubmissionEvidence(SubmissionState):
    def __init__(self, client: BybitPrivateReadClient, client_order_id: str): self.client, self.client_order_id = client, client_order_id
    def decision_state(self, decision_id: str) -> str: return "none"
    def client_order_state(self, client_order_id: str) -> str:
        if client_order_id != self.client_order_id: return "none"
        return self.client.submission_state(client_order_id)


class BybitPostAckReconciler:
    """Fresh post-ACK read-back adapter; never reuses pre-submit evidence."""
    def __init__(self, client: BybitPrivateReadClient): self.client = client
    def reconcile_after_ack(self, client_order_id: str):
        # The execution engine accepts this richer object and only creates an
        # Execution from its authoritative fill rows.  Keep the legacy string
        # return for clients that do not expose fill details.
        from .live_order import ConfirmedFill, ReconciliationEvidence
        order = self.client.lookup_order(client_order_id)
        fills = self.client.executions(client_order_id)
        if order.order_id and any(fill.order_id != order.order_id for fill in fills):
            return ReconciliationEvidence("ambiguous")
        if order.state in {OrderState.ACTIVE, OrderState.PARTIALLY_FILLED, OrderState.AMBIGUOUS}:
            return ReconciliationEvidence("ambiguous", order.order_id)
        if order.state is not OrderState.FILLED or not fills:
            return ReconciliationEvidence("ambiguous", order.order_id)
        converted = tuple(ConfirmedFill(fill.exec_id, fill.order_id, fill.order_link_id, fill.exec_qty, fill.exec_value, fill.exec_price, fill.exec_time, fill.exec_fee, "", fill.category, fill.symbol) for fill in fills)
        return ReconciliationEvidence("confirmed", order.order_id, converted)


# Descriptive alias for callers that do not need to distinguish the transport
# from the read-only client implementation.
PrivateBybitClient = BybitPrivateReadClient
