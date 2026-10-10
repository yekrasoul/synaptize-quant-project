"""Phase 5.5A/5.5B authenticated Bybit Spot transport.

The POST capability added in Phase 5.5B is behind an independent, ordered
execution-gate check and a one-shot idempotency registry. Production
configuration keeps that boundary disabled. There are no amend or cancel
methods.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import socket
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit

from .market_data.http import HttpResponse

BYBIT_BASE_URL = "https://api.bybit.com"
API_KEY_ENV = "BYBIT_API_KEY"
API_SECRET_ENV = "BYBIT_API_SECRET"
RECV_WINDOW = "5000"

USER_QUERY_API = "/v5/user/query-api"
ACCOUNT_INFO = "/v5/account/info"
WALLET_BALANCE = "/v5/account/wallet-balance"
ORDER_REALTIME = "/v5/order/realtime"
ORDER_HISTORY = "/v5/order/history"
EXECUTION_LIST = "/v5/execution/list"
INSTRUMENTS_INFO = "/v5/market/instruments-info"
SERVER_TIME = "/v5/market/time"

AUTHENTICATED_GET_PATHS = frozenset({
    USER_QUERY_API, ACCOUNT_INFO, WALLET_BALANCE,
    ORDER_REALTIME, ORDER_HISTORY, EXECUTION_LIST,
})
PUBLIC_GET_PATHS = frozenset({INSTRUMENTS_INFO, SERVER_TIME})


class BybitSpotTransportError(ValueError):
    """Base class for safe, secret-free transport errors."""


class BybitCredentialsUnavailable(BybitSpotTransportError):
    """Required environment credentials are absent or blank."""


class BybitEndpointRejected(BybitSpotTransportError):
    """The requested endpoint is not in the read-only allowlist."""


class BybitAuthenticationFailure(BybitSpotTransportError):
    """Bybit rejected authenticated evidence."""


class BybitHttpFailure(BybitSpotTransportError):
    """The HTTP response was unsuccessful or unavailable."""


class BybitMalformedResponse(BybitSpotTransportError):
    """The response did not satisfy the Bybit V5 envelope contract."""


class BybitSubmissionAmbiguous(BybitSpotTransportError):
    """The network boundary may have been crossed; reconciliation is required."""


@dataclass(frozen=True)
class BybitCredentials:
    """Environment-sourced credentials; the secret is never represented."""

    api_key: str
    _api_secret: str = field(repr=False)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "BybitCredentials":
        values = os.environ if environ is None else environ
        api_key = values.get(API_KEY_ENV, "")
        api_secret = values.get(API_SECRET_ENV, "")
        if not api_key.strip() or not api_secret.strip():
            raise BybitCredentialsUnavailable(f"{API_KEY_ENV} and {API_SECRET_ENV} must be set")
        return cls(api_key, api_secret)

    def __repr__(self) -> str:
        return "BybitCredentials(api_key='<redacted>', api_secret='<redacted>')"


@dataclass(frozen=True)
class SpotMarketBuyRequest:
    """Pure future-order model; creating it performs no network operation."""

    quote_amount_usdt: Decimal
    client_order_id: str
    category: str = "spot"
    symbol: str = "BTCUSDT"
    side: str = "Buy"
    order_type: str = "Market"
    market_unit: str = "quoteCoin"
    is_leverage: int = 0
    order_filter: str = "Order"

    def __post_init__(self) -> None:
        try:
            amount = Decimal(str(self.quote_amount_usdt))
        except (InvalidOperation, ValueError) as exc:
            raise BybitSpotTransportError("quote amount is invalid") from exc
        if not amount.is_finite() or amount <= 0:
            raise BybitSpotTransportError("quote amount must be positive")
        if (self.category, self.symbol, self.side, self.order_type, self.market_unit, self.is_leverage, self.order_filter) != (
            "spot", "BTCUSDT", "Buy", "Market", "quoteCoin", 0, "Order"
        ):
            raise BybitSpotTransportError("only Bybit Spot BTCUSDT quoteCoin Market Buy is supported")
        if not self.client_order_id or len(self.client_order_id) > 36:
            raise BybitSpotTransportError("client order ID is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "symbol": self.symbol,
            "side": self.side,
            "orderType": self.order_type,
            "qty": format(Decimal(str(self.quote_amount_usdt)).normalize(), "f"),
            "marketUnit": self.market_unit,
            "isLeverage": self.is_leverage,
            "orderLinkId": self.client_order_id,
            "orderFilter": self.order_filter,
        }


@dataclass(frozen=True)
class ExecutionGateState:
    """The complete, current runtime authorization snapshot required by POST."""

    live_execution_enabled: bool
    kill_switch: bool
    order_submission: str
    production_enablement: str
    authorization: str
    activation: str
    execution: str
    market: str
    exchange: str = "Bybit"
    market_type: str = "spot"
    symbol: str = "BTCUSDT"

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ExecutionGateState":
        required = (
            "live_execution_enabled", "kill_switch", "order_submission",
            "production_enablement", "authorization", "activation", "execution", "market",
        )
        if any(key not in values for key in required):
            raise BybitSpotTransportError("complete execution gate state is required")
        return cls(
            live_execution_enabled=values["live_execution_enabled"],
            kill_switch=values["kill_switch"],
            order_submission=str(values["order_submission"]),
            production_enablement=str(values["production_enablement"]),
            authorization=str(values["authorization"]),
            activation=str(values["activation"]),
            execution=str(values["execution"]),
            market=str(values["market"]),
            exchange=str(values.get("exchange", "Bybit")),
            market_type=str(values.get("market_type", "spot")),
            symbol=str(values.get("symbol", "BTCUSDT")),
        )

    def blocked_reason(self, request: SpotMarketBuyRequest) -> str | None:
        checks = (
            (self.live_execution_enabled is True, "live_execution_enabled is false"),
            (self.kill_switch is False, "kill_switch is true"),
            (self.order_submission == "implemented", "order_submission does not permit implemented submission"),
            (self.production_enablement == "ENABLED", "Production Enablement is not ENABLED"),
            (self.authorization == "AUTHORIZED", "Authorization is not AUTHORIZED"),
            (self.activation == "ACTIVATED", "Activation is not ACTIVATED"),
            (self.execution == "ALLOWED", "Execution is not ALLOWED"),
            (self.market == "Bybit Spot BTCUSDT", "market is not Bybit Spot BTCUSDT"),
            (self.exchange == "Bybit", "exchange is not Bybit"),
            (self.market_type == "spot", "market type is not Spot"),
            (self.symbol == "BTCUSDT", "symbol is not BTCUSDT"),
            (request.category == "spot", "request category is not Spot"),
            (request.symbol == "BTCUSDT", "request symbol is not BTCUSDT"),
        )
        for valid, reason in checks:
            if not valid:
                return reason
        return None


@dataclass(frozen=True)
class PreparedBybitRequest:
    """Signed request bytes/headers with no method that sends them."""

    method: str
    path: str
    query: str
    body: str
    headers: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class BybitEnvelope:
    http_status: int
    ret_code: int | None
    ret_msg: str | None
    result: Mapping[str, Any] | None


@dataclass(frozen=True)
class AuthenticationCheck:
    status: str
    endpoint: str
    http_status: int | None = None
    ret_code: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class OrderAcknowledgement:
    """Bybit submission acceptance; this is not fill confirmation."""

    order_id: str
    order_link_id: str
    ret_code: int
    ret_msg: str
    http_status: int


@dataclass(frozen=True)
class OrderSubmissionResult:
    status: str
    reason: str
    acknowledgement: OrderAcknowledgement | None = None


class SubmissionAttemptRegistry:
    """Process-scoped one-shot guard; durable attempts remain the engine's job."""

    def __init__(self) -> None:
        self._claimed: set[str] = set()

    def claim(self, identity: str) -> bool:
        if identity in self._claimed:
            return False
        self._claimed.add(identity)
        return True


def canonical_query(params: Mapping[str, str | int]) -> str:
    return urlencode(sorted((str(key), str(value)) for key, value in params.items()))


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sign_query(timestamp: str, api_key: str, recv_window: str, query: str, api_secret: str) -> str:
    message = f"{timestamp}{api_key}{recv_window}{query}"
    return hmac.new(api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_json_body(timestamp: str, api_key: str, recv_window: str, body: str, api_secret: str) -> str:
    message = f"{timestamp}{api_key}{recv_window}{body}"
    return hmac.new(api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def deterministic_client_order_id(strategy_id: str, decision_id: str, run_id: str) -> str:
    """Return the existing stable V1 identity without adding a new identity scheme."""
    return "dca-" + hashlib.sha256(f"{strategy_id}|{decision_id}|{run_id}".encode("utf-8")).hexdigest()[:32]


def request_fingerprint(request: SpotMarketBuyRequest) -> str:
    return hashlib.sha256(canonical_json(request.to_payload()).encode("utf-8")).hexdigest()


class BybitSpotRestTransport:
    """Authenticated read transport and guarded submission boundary.

    ``get`` is limited to named GET endpoints. ``submit_order`` is the only
    POST path and requires every explicit execution gate.
    """

    def __init__(
        self,
        credentials: BybitCredentials,
        *,
        base_url: str = BYBIT_BASE_URL,
        clock: Callable[[], int] | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise BybitSpotTransportError("Bybit base URL must be HTTPS")
        if timeout_seconds <= 0 or not 1 <= max_attempts <= 5:
            raise BybitSpotTransportError("transport timeout or retry policy is invalid")
        self._credentials = credentials
        self._host, self._port, self._base_path = parsed.hostname, parsed.port, parsed.path.rstrip("/")
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._submission_attempts = SubmissionAttemptRegistry()

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "BybitSpotRestTransport":
        return cls(BybitCredentials.from_environment(), **kwargs)

    def prepare_authenticated_get(self, path: str, params: Mapping[str, str | int] = {}) -> PreparedBybitRequest:
        if path not in AUTHENTICATED_GET_PATHS:
            raise BybitEndpointRejected("endpoint is not an approved authenticated GET")
        query = canonical_query(params)
        timestamp = str(self._clock())
        return PreparedBybitRequest(
            "GET", path, query, "", self._headers(timestamp, sign_query(timestamp, self._credentials.api_key, RECV_WINDOW, query, self._credentials._api_secret))
        )

    def prepare_market_buy_request(self, request: SpotMarketBuyRequest) -> PreparedBybitRequest:
        """Prepare a future exact request without sending or exposing a sender."""
        body = canonical_json(request.to_payload())
        timestamp = str(self._clock())
        return PreparedBybitRequest(
            "POST", "/v5/order/create", "", body,
            self._headers(timestamp, sign_json_body(timestamp, self._credentials.api_key, RECV_WINDOW, body, self._credentials._api_secret), content_type=True),
        )

    def get(self, path: str, params: Mapping[str, str | int] = {}) -> HttpResponse:
        prepared = self.prepare_authenticated_get(path, params)
        return self._get_once_or_retry(prepared)

    def check_authentication(self) -> AuthenticationCheck:
        try:
            response = self.get(USER_QUERY_API)
            envelope = normalize_response(response)
            if envelope.ret_code != 0:
                return AuthenticationCheck("ALERT", USER_QUERY_API, envelope.http_status, envelope.ret_code, envelope.ret_msg or "Bybit authentication rejected")
            return AuthenticationCheck("HEALTHY", USER_QUERY_API, envelope.http_status, envelope.ret_code)
        except BybitAuthenticationFailure as exc:
            return AuthenticationCheck("ALERT", USER_QUERY_API, reason=str(exc))
        except BybitSpotTransportError as exc:
            return AuthenticationCheck("UNKNOWN", USER_QUERY_API, reason=str(exc))

    def submission_reconciler(self) -> Any:
        """Build the read-only order/fill reconciler without adding a write path."""
        from .bybit_submission import BybitSubmissionReconciler
        from .private_bybit import BybitPrivateReadClient

        transport = self

        class _ReadAdapter:
            def get(self, path: str, params: Mapping[str, str], headers: Mapping[str, str]) -> HttpResponse:
                return transport.get(path, params)

        return BybitSubmissionReconciler(BybitPrivateReadClient(_ReadAdapter()))

    def submit_order(
        self,
        request: SpotMarketBuyRequest,
        *,
        gate_state: ExecutionGateState | Mapping[str, Any] | None = None,
        gate_provider: Callable[[], ExecutionGateState | Mapping[str, Any]] | None = None,
        attempt_registry: SubmissionAttemptRegistry | None = None,
        journal: Any | None = None,
        submission_context: Any | None = None,
        attempt_id: str | None = None,
        now: Any | None = None,
    ) -> OrderSubmissionResult:
        """Submit one approved request, or return a deterministic block.

        A provider is preferred for production callers so the gate snapshot is
        independently re-read immediately before the network boundary.
        """
        if gate_provider is None and gate_state is None:
            return OrderSubmissionResult("BLOCKED", "execution gate state is unavailable")

        def current_gates() -> ExecutionGateState:
            raw = gate_provider() if gate_provider is not None else gate_state
            if isinstance(raw, ExecutionGateState):
                return raw
            if isinstance(raw, Mapping):
                return ExecutionGateState.from_mapping(raw)
            raise BybitSpotTransportError("execution gate state is malformed")

        try:
            reason = current_gates().blocked_reason(request)
            if reason is not None:
                return OrderSubmissionResult("BLOCKED", reason)
            prepared = self.prepare_market_buy_request(request)
            # Re-read all gates after request construction and immediately
            # before POST. A stale first snapshot cannot authorize submission.
            reason = current_gates().blocked_reason(request)
            if reason is not None:
                return OrderSubmissionResult("BLOCKED", reason)
        except (BybitSpotTransportError, TypeError, ValueError) as exc:
            return OrderSubmissionResult("BLOCKED", str(exc))

        # A gated POST is never permitted without durable pre-network
        # evidence.  The import is local to keep the read/preparation module
        # free of an import cycle and to preserve its pure request API.
        if journal is None or submission_context is None or not attempt_id or now is None:
            return OrderSubmissionResult("BLOCKED", "durable submission journal and immutable attempt context are required")
        try:
            if hasattr(journal, "has_unresolved_attempt") and journal.has_unresolved_attempt():
                return OrderSubmissionResult("BLOCKED", "an unresolved submission attempt exists; reconcile before any new submission")
            journal.persist_before_post(request, submission_context, attempt_id=attempt_id, now=now)
        except Exception as exc:
            return OrderSubmissionResult("BLOCKED", str(exc))

        registry = attempt_registry or self._submission_attempts
        identity = request_fingerprint(request)
        if not registry.claim(identity):
            return OrderSubmissionResult("BLOCKED", "submission attempt already claimed; reconciliation required")
        try:
            response = self._post_order(prepared)
            envelope = normalize_response(response)
            if envelope.ret_code != 0:
                result = OrderSubmissionResult("REJECTED", envelope.ret_msg or "Bybit rejected order submission")
                journal.persist_outcome(request, submission_context, attempt_id=attempt_id, now=now, state=result.status, ret_code=envelope.ret_code, ret_msg=envelope.ret_msg, order_id=None, reconciliation_state="REJECTED")
                return result
            result = envelope.result or {}
            order_id = result.get("orderId")
            order_link_id = result.get("orderLinkId")
            if not isinstance(order_id, str) or not isinstance(order_link_id, str) or order_link_id != request.client_order_id:
                outcome = OrderSubmissionResult("RECONCILIATION_REQUIRED", "Bybit acknowledgement did not match the client order identity")
                journal.persist_outcome(request, submission_context, attempt_id=attempt_id, now=now, state=outcome.status, ret_code=envelope.ret_code, ret_msg="acknowledgement identity mismatch", order_id=order_id if isinstance(order_id, str) else None, reconciliation_state="UNRESOLVED")
                return outcome
            outcome = OrderSubmissionResult(
                "ACCEPTED",
                "Bybit accepted the submission; reconciliation is still required",
                OrderAcknowledgement(order_id, order_link_id, envelope.ret_code, envelope.ret_msg or "", envelope.http_status),
            )
            journal.persist_outcome(request, submission_context, attempt_id=attempt_id, now=now, state=outcome.status, ret_code=envelope.ret_code, ret_msg=envelope.ret_msg, order_id=order_id, reconciliation_state="ACCEPTED")
            return outcome
        except BybitAuthenticationFailure as exc:
            outcome = OrderSubmissionResult("REJECTED", str(exc))
            journal.persist_outcome(request, submission_context, attempt_id=attempt_id, now=now, state=outcome.status, ret_code=None, ret_msg=str(exc), order_id=None, reconciliation_state="REJECTED")
            return outcome
        except (BybitSubmissionAmbiguous, BybitHttpFailure, socket.timeout, TimeoutError, OSError):
            outcome = OrderSubmissionResult("RECONCILIATION_REQUIRED", "submission outcome is ambiguous; reconcile before retry")
            journal.persist_outcome(request, submission_context, attempt_id=attempt_id, now=now, state=outcome.status, ret_code=None, ret_msg="submission outcome ambiguous", order_id=None, reconciliation_state="SUBMISSION_UNKNOWN")
            return outcome
        except BybitMalformedResponse:
            outcome = OrderSubmissionResult("RECONCILIATION_REQUIRED", "submission response is malformed; reconcile before retry")
            journal.persist_outcome(request, submission_context, attempt_id=attempt_id, now=now, state=outcome.status, ret_code=None, ret_msg="submission response malformed", order_id=None, reconciliation_state="UNRESOLVED")
            return outcome

    def _post_order(self, prepared: PreparedBybitRequest) -> HttpResponse:
        if prepared.method != "POST" or prepared.path != "/v5/order/create":
            raise BybitEndpointRejected("only Spot order creation is permitted")
        connection = http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout_seconds)
        try:
            connection.request("POST", f"{self._base_path}{prepared.path}", body=prepared.body.encode("utf-8"), headers=dict(prepared.headers))
            raw = connection.getresponse()
            return HttpResponse(raw.status, {key.lower(): value for key, value in raw.getheaders()}, raw.read())
        except (socket.timeout, TimeoutError, OSError) as exc:
            raise BybitSubmissionAmbiguous("submission outcome is ambiguous; reconcile before retry") from exc
        finally:
            connection.close()

    def _headers(self, timestamp: str, request_signature: str, *, content_type: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-BAPI-API-KEY": self._credentials.api_key,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": request_signature,
        }
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def _get_once_or_retry(self, prepared: PreparedBybitRequest) -> HttpResponse:
        target = f"{self._base_path}{prepared.path}" + (f"?{prepared.query}" if prepared.query else "")
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            connection = http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout_seconds)
            try:
                connection.request("GET", target, headers=dict(prepared.headers))
                raw = connection.getresponse()
                response = HttpResponse(raw.status, {key.lower(): value for key, value in raw.getheaders()}, raw.read())
                if response.status != 429 and response.status < 500:
                    return response
                if attempt == self._max_attempts:
                    return response
            except (socket.timeout, TimeoutError) as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    raise BybitHttpFailure("Bybit authenticated read timed out") from exc
            except OSError as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    raise BybitHttpFailure("Bybit authenticated read unavailable") from exc
            finally:
                connection.close()
            self._sleep(0.2 * (2 ** (attempt - 1)))
        raise BybitHttpFailure("Bybit authenticated read unavailable") from last_error


def normalize_response(response: HttpResponse) -> BybitEnvelope:
    if response.status in {401, 403}:
        raise BybitAuthenticationFailure("Bybit authentication failed")
    if response.status >= 400:
        raise BybitHttpFailure(f"Bybit HTTP request failed with status {response.status}")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BybitMalformedResponse("Bybit response is not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("retCode"), int):
        raise BybitMalformedResponse("Bybit response envelope is malformed")
    result = payload.get("result")
    if result is not None and not isinstance(result, dict):
        raise BybitMalformedResponse("Bybit response result is malformed")
    ret_msg = payload.get("retMsg")
    if ret_msg is not None and not isinstance(ret_msg, str):
        raise BybitMalformedResponse("Bybit response message is malformed")
    if payload["retCode"] in {10003, 10004, 10005}:
        raise BybitAuthenticationFailure("Bybit authenticated read was rejected")
    return BybitEnvelope(response.status, payload["retCode"], ret_msg, result)
