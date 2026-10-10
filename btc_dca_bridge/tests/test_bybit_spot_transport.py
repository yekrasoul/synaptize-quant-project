import hashlib
import hmac
import json
import unittest
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.bybit_spot_transport import (
    API_KEY_ENV,
    API_SECRET_ENV,
    USER_QUERY_API,
    BybitAuthenticationFailure,
    BybitCredentials,
    BybitCredentialsUnavailable,
    BybitEndpointRejected,
    BybitSpotRestTransport,
    ExecutionGateState,
    HttpResponse,
    SpotMarketBuyRequest,
    SubmissionAttemptRegistry,
    deterministic_client_order_id,
    normalize_response,
    request_fingerprint,
    sign_json_body,
    sign_query,
)
from btc_dca_bridge.bybit_submission import DurableSubmissionJournal, SubmissionContext


class _RawResponse:
    status = 200
    body = b'{"retCode":0,"retMsg":"OK","result":{"id":"safe"}}'

    def getheaders(self):
        return [("Content-Type", "application/json")]

    def read(self):
        return self.body


class _Connection:
    calls = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def request(self, method, target, headers=None, body=None):
        self.calls.append((method, target, headers, body))

    def getresponse(self):
        return _RawResponse()

    def close(self):
        pass


class _TimeoutConnection(_Connection):
    def getresponse(self):
        raise TimeoutError("simulated timeout")


class BybitSpotTransportTests(unittest.TestCase):
    def setUp(self):
        _Connection.calls = []
        _RawResponse.body = b'{"retCode":0,"retMsg":"OK","result":{"id":"safe"}}'
        self.credentials = BybitCredentials("test-key", "test-secret")

    @staticmethod
    def gates(**overrides):
        values = {
            "live_execution_enabled": True,
            "kill_switch": False,
            "order_submission": "implemented",
            "production_enablement": "ENABLED",
            "authorization": "AUTHORIZED",
            "activation": "ACTIVATED",
            "execution": "ALLOWED",
            "market": "Bybit Spot BTCUSDT",
        }
        values.update(overrides)
        return values

    def test_environment_credentials_are_required_and_secret_is_not_repr(self):
        with self.assertRaises(BybitCredentialsUnavailable):
            BybitCredentials.from_environment({})
        loaded = BybitCredentials.from_environment({API_KEY_ENV: "key", API_SECRET_ENV: "secret-value"})
        self.assertNotIn("secret-value", repr(loaded))
        self.assertEqual(loaded.api_key, "key")

    def test_v5_query_and_json_signatures_are_deterministic(self):
        query = "category=spot&symbol=BTCUSDT"
        expected_query = hmac.new(b"secret", b"1700000000000key5000category=spot&symbol=BTCUSDT", hashlib.sha256).hexdigest()
        self.assertEqual(sign_query("1700000000000", "key", "5000", query, "secret"), expected_query)
        body = '{"category":"spot","symbol":"BTCUSDT"}'
        expected_body = hmac.new(b"secret", ("1700000000000key5000" + body).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sign_json_body("1700000000000", "key", "5000", body, "secret"), expected_body)

    def test_authenticated_get_is_signed_and_only_gets(self):
        transport = BybitSpotRestTransport(self.credentials, clock=lambda: 1700000000000, sleep=lambda _: None)
        with patch("btc_dca_bridge.bybit_spot_transport.http.client.HTTPSConnection", _Connection):
            response = transport.get(USER_QUERY_API)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(_Connection.calls), 1)
        method, target, headers, body = _Connection.calls[0]
        self.assertEqual(method, "GET")
        self.assertIsNone(body)
        self.assertIn("X-BAPI-SIGN", headers)
        self.assertNotIn("test-secret", json.dumps(headers))
        self.assertEqual(target, "/v5/user/query-api")

    def test_allowlist_rejects_order_creation_and_other_mutating_paths(self):
        transport = BybitSpotRestTransport(self.credentials)
        with self.assertRaises(BybitEndpointRejected):
            transport.get("/v5/order/create")

    def test_authentication_check_is_read_only_and_normalized(self):
        transport = BybitSpotRestTransport(self.credentials, clock=lambda: 1700000000000, sleep=lambda _: None)
        with patch("btc_dca_bridge.bybit_spot_transport.http.client.HTTPSConnection", _Connection):
            result = transport.check_authentication()
        self.assertEqual(result.status, "HEALTHY")
        self.assertEqual(result.endpoint, USER_QUERY_API)
        self.assertEqual(_Connection.calls[0][0], "GET")

    def test_authentication_rejection_does_not_leak_credentials(self):
        response = HttpResponse(200, {}, b'{"retCode":10004,"retMsg":"bad signature","result":{}}')
        with self.assertRaises(BybitAuthenticationFailure) as raised:
            normalize_response(response)
        self.assertNotIn("test-secret", str(raised.exception))

    def test_exact_spot_market_buy_model_and_pure_signed_preparation(self):
        request = SpotMarketBuyRequest(Decimal("25"), deterministic_client_order_id("btc_adaptive_dca_v1", "decision", "run"))
        self.assertEqual(request.to_payload(), {
            "category": "spot", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market",
            "qty": "25", "marketUnit": "quoteCoin", "isLeverage": 0,
            "orderLinkId": request.client_order_id, "orderFilter": "Order",
        })
        transport = BybitSpotRestTransport(self.credentials, clock=lambda: 1700000000000)
        prepared = transport.prepare_market_buy_request(request)
        self.assertEqual(prepared.method, "POST")
        self.assertEqual(prepared.path, "/v5/order/create")
        self.assertEqual(request_fingerprint(request), request_fingerprint(request))
        self.assertNotIn("test-secret", prepared.body)

    def test_market_contract_and_id_are_deterministic(self):
        client_id = deterministic_client_order_id("btc_adaptive_dca_v1", "d", "r")
        self.assertEqual(client_id, deterministic_client_order_id("btc_adaptive_dca_v1", "d", "r"))
        self.assertNotEqual(client_id, deterministic_client_order_id("btc_adaptive_dca_v1", "d2", "r"))
        with self.assertRaises(ValueError):
            SpotMarketBuyRequest(Decimal("25"), "id", symbol="ETHUSDT")

    def test_every_blocked_gate_makes_zero_post_calls(self):
        request = SpotMarketBuyRequest(Decimal("25"), "dca-" + "a" * 32)
        blocked = (
            {"live_execution_enabled": False},
            {"kill_switch": True},
            {"order_submission": "not_implemented"},
            {"production_enablement": "NOT_ENABLED"},
            {"authorization": "NOT_AUTHORIZED"},
            {"activation": "NOT_ACTIVATED"},
            {"execution": "BLOCKED"},
            {"market": "Bybit Spot ETHUSDT"},
        )
        with patch("btc_dca_bridge.bybit_spot_transport.http.client.HTTPSConnection", _Connection):
            for override in blocked:
                result = BybitSpotRestTransport(self.credentials).submit_order(request, gate_state=self.gates(**override))
                self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(_Connection.calls, [])

    def test_missing_or_malformed_gate_state_blocks_without_post(self):
        request = SpotMarketBuyRequest(Decimal("25"), "dca-" + "b" * 32)
        transport = BybitSpotRestTransport(self.credentials)
        self.assertEqual(transport.submit_order(request).status, "BLOCKED")
        self.assertEqual(transport.submit_order(request, gate_state={}).status, "BLOCKED")
        self.assertEqual(_Connection.calls, [])

    def test_gate_provider_is_reread_immediately_before_post(self):
        request = SpotMarketBuyRequest(Decimal("25"), "dca-" + "c" * 32)
        states = iter((self.gates(), self.gates(kill_switch=True)))
        transport = BybitSpotRestTransport(self.credentials)
        result = transport.submit_order(request, gate_provider=lambda: next(states))
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(_Connection.calls, [])

    def test_enabled_fake_flow_posts_once_and_normalizes_ack_only(self):
        request = SpotMarketBuyRequest(Decimal("25"), "dca-" + "d" * 32)
        _RawResponse.body = json.dumps({"retCode": 0, "retMsg": "OK", "result": {"orderId": "order-1", "orderLinkId": request.client_order_id}}).encode()
        transport = BybitSpotRestTransport(self.credentials, clock=lambda: 1700000000000)
        with tempfile.TemporaryDirectory() as root:
            journal = DurableSubmissionJournal(root)
            context = SubmissionContext("canary-" + "1" * 32, "approval-" + "2" * 32, "3" * 64, "decision", "intent", "run_20261010T170000Z_context01")
            with patch("btc_dca_bridge.bybit_spot_transport.http.client.HTTPSConnection", _Connection):
                result = transport.submit_order(request, gate_state=ExecutionGateState.from_mapping(self.gates()), journal=journal, submission_context=context, attempt_id="attempt-1", now=datetime(2026, 10, 10, 17, tzinfo=UTC))
        self.assertEqual(result.status, "ACCEPTED")
        self.assertEqual(result.acknowledgement.order_id, "order-1")
        self.assertEqual(result.acknowledgement.order_link_id, request.client_order_id)
        self.assertIn("reconciliation", result.reason)
        self.assertEqual(len(_Connection.calls), 1)
        method, target, headers, body = _Connection.calls[0]
        self.assertEqual((method, target), ("POST", "/v5/order/create"))
        self.assertEqual(json.loads(body), request.to_payload())
        self.assertNotIn("test-secret", body.decode())

    def test_same_attempt_is_idempotently_blocked_without_second_post(self):
        request = SpotMarketBuyRequest(Decimal("25"), "dca-" + "e" * 32)
        _RawResponse.body = json.dumps({"retCode": 0, "retMsg": "OK", "result": {"orderId": "order-2", "orderLinkId": request.client_order_id}}).encode()
        registry = SubmissionAttemptRegistry()
        transport = BybitSpotRestTransport(self.credentials)
        with tempfile.TemporaryDirectory() as root:
            journal = DurableSubmissionJournal(root)
            context = SubmissionContext("canary-" + "4" * 32, "approval-" + "5" * 32, "6" * 64, "decision", "intent", "run_20261010T170000Z_context02")
            with patch("btc_dca_bridge.bybit_spot_transport.http.client.HTTPSConnection", _Connection):
                first = transport.submit_order(request, gate_state=self.gates(), attempt_registry=registry, journal=journal, submission_context=context, attempt_id="attempt-2", now=datetime(2026, 10, 10, 17, tzinfo=UTC))
                second = transport.submit_order(request, gate_state=self.gates(), attempt_registry=registry, journal=journal, submission_context=context, attempt_id="attempt-2", now=datetime(2026, 10, 10, 17, tzinfo=UTC))
        self.assertEqual(first.status, "ACCEPTED")
        self.assertEqual(second.status, "BLOCKED")
        self.assertEqual(len(_Connection.calls), 1)

    def test_timeout_persists_attempt_and_ambiguous_outcome_before_retry_can_be_considered(self):
        request = SpotMarketBuyRequest(Decimal("25"), "dca-" + "1" * 32)
        transport = BybitSpotRestTransport(self.credentials, max_attempts=1)
        with tempfile.TemporaryDirectory() as root:
            journal = DurableSubmissionJournal(root)
            context = SubmissionContext("canary-" + "1" * 32, "approval-" + "2" * 32, "3" * 64, "decision", "intent", "run_20261010T170000Z_context03")
            with patch("btc_dca_bridge.bybit_spot_transport.http.client.HTTPSConnection", _TimeoutConnection):
                result = transport.submit_order(request, gate_state=self.gates(), journal=journal, submission_context=context, attempt_id="attempt-3", now=datetime(2026, 10, 10, 17, tzinfo=UTC))
            self.assertEqual(result.status, "RECONCILIATION_REQUIRED")
            self.assertTrue(journal.has_prior_attempt(request.client_order_id))
            self.assertEqual(len(list((Path(root) / "order_submission_outcomes").rglob("*.json"))), 1)

    def test_blocked_submission_does_not_mutate_canonical_ledger(self):
        from pathlib import Path
        ledger = Path(__file__).parents[1] / "ledger" / "executions.jsonl"
        before = ledger.read_bytes()
        result = BybitSpotRestTransport(self.credentials).submit_order(
            SpotMarketBuyRequest(Decimal("25"), "dca-" + "f" * 32), gate_state=self.gates(kill_switch=True)
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(ledger.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
