"""No-network characterization of the V2 CCXT -> Bybit order boundary."""
from __future__ import annotations

import json
import socket
from decimal import Decimal
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
import unittest
from unittest.mock import patch

import ccxt

from btc_dca_bridge.core.models import AccountIdentity, Asset, Market, OrderIntent, OrderType, Side
from btc_dca_bridge.core.state import ExecutionState
from btc_dca_bridge.execution_v2.oms import InMemoryOms
from btc_dca_bridge.execution_v2.service import ExecutionService
from btc_dca_bridge.risk.engine import MarketEvidenceBinding, RiskContext, RiskEngine
from btc_dca_bridge.venues.ccxt.adapter import CcxtVenueAdapter
from btc_dca_bridge.cli import main


def intent() -> OrderIntent:
    return OrderIntent(
        "wire-intent", "adaptive_dca_v1", "wire-decision",
        Market(Asset("BTC"), Asset("USDT")), Side.BUY, OrderType.MARKET,
        Decimal("25"), "bybit", "wire-client-identity", "2026-10-24T00:00:00Z",
        "market-wire", "a" * 64, "sentiment-wire", "b" * 64, 35,
    )


def context() -> RiskContext:
    return RiskContext(
        calendar_month="2026-10", monthly_spend=Decimal("0"), monthly_cap=Decimal("500"),
        runtime_execution_enabled=True, kill_switch_active=False, market_data_fresh=True,
        account_identity=AccountIdentity("bybit", "account-1"), expected_account_fingerprint="account-1",
        quote_balance=Decimal("500"), unresolved_execution_exists=False,
        duplicate_execution_exists=False, venue_health="healthy",
        market_evidence=MarketEvidenceBinding("wire-decision", "market-wire", "a" * 64, "sentiment-wire", "b" * 64, True, 35, Decimal("25")),
    )


class RecordingBybit:
    """Actual CCXT Bybit request construction with its final POST method replaced."""

    def __init__(self, outcome: BaseException | None = None) -> None:
        self.requests: list[dict[str, object]] = []
        self.outcome = outcome
        self.exchange = ccxt.bybit({"options": {"defaultType": "spot"}})
        market = {
            "id": "BTCUSDT", "symbol": "BTC/USDT", "base": "BTC", "quote": "USDT",
            "baseId": "BTC", "quoteId": "USDT", "settle": None, "type": "spot",
            "spot": True, "margin": False, "swap": False, "future": False, "option": False,
            "contract": False, "linear": False, "inverse": False, "active": True,
            "precision": {"amount": 0.000001, "price": 0.01, "cost": 0.01},
            "limits": {"amount": {"min": 0.00001, "max": None}, "price": {"min": None, "max": None}, "cost": {"min": 10, "max": None}},
        }
        self.exchange.markets = {"BTC/USDT": market}
        self.exchange.markets_by_id = {"BTCUSDT": [market]}
        # This probe must not authenticate or query an account. It fixes only
        # the account-mode input normally obtained by CCXT's read-only probe.
        self.exchange.is_unified_enabled = lambda: (True, True)
        self.exchange.privatePostV5OrderCreate = self._record_post  # type: ignore[method-assign]
        self.exchange.fetch_order = lambda order_id, _symbol: {"id": order_id, "status": "closed"}  # type: ignore[method-assign]
        self.exchange.fetch_my_trades = lambda _symbol, params: []  # type: ignore[method-assign]

    def _record_post(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(dict(request))
        if self.outcome is not None:
            raise self.outcome
        return {"retCode": 0, "result": {"orderId": "wire-order-1", "orderLinkId": "wire-client-identity"}}


class SignedWireRecorder:
    """Capture CCXT's signed request immediately before its HTTP transport."""

    def __init__(self) -> None:
        self.api_key = "fixture-api-key"
        self.secret = "fixture-secret"
        self.requests: list[dict[str, object]] = []
        self.exchange = ccxt.bybit({
            "apiKey": self.api_key,
            "secret": self.secret,
            "options": {"defaultType": "spot"},
        })
        market = {
            "id": "BTCUSDT", "symbol": "BTC/USDT", "base": "BTC", "quote": "USDT",
            "baseId": "BTC", "quoteId": "USDT", "settle": None, "type": "spot",
            "spot": True, "margin": False, "swap": False, "future": False, "option": False,
            "contract": False, "linear": False, "inverse": False, "active": True,
            "precision": {"amount": 0.000001, "price": 0.01, "cost": 0.01},
            "limits": {"amount": {"min": 0.00001, "max": None}, "price": {"min": None, "max": None}, "cost": {"min": 10, "max": None}},
        }
        self.exchange.markets = {"BTC/USDT": market}
        self.exchange.markets_by_id = {"BTCUSDT": [market]}
        self.exchange.is_unified_enabled = lambda: (True, True)
        self.exchange.nonce = lambda: 1_700_000_000_000
        self.exchange.fetch = self._record_fetch  # type: ignore[method-assign]

    def _record_fetch(self, url, method="GET", headers=None, body=None):
        self.requests.append({"url": url, "method": method, "headers": dict(headers or {}), "body": body})
        return {"retCode": 0, "result": {"orderId": "wire-order-1", "orderLinkId": "wire-client-identity"}}


class CcxtWireBoundaryTests(unittest.TestCase):
    def _service(self, recorder: RecordingBybit) -> ExecutionService:
        venue = CcxtVenueAdapter("bybit", recorder.exchange, identity_factory=lambda _: "account-1")
        return ExecutionService(oms=InMemoryOms(), risk_engine=RiskEngine(), venue=venue)

    def test_execution_service_reaches_one_real_ccxt_bybit_request_builder(self):
        recorder = RecordingBybit()
        service = self._service(recorder)

        result = service.execute(intent(), context())
        duplicate = service.execute(intent(), context())

        self.assertEqual(result.order.state, ExecutionState.CONFIRMED)
        self.assertEqual(duplicate.risk_status, "DUPLICATE_OR_RECONCILIATION_REQUIRED")
        self.assertEqual(recorder.requests, [{
            "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "category": "spot",
            "marketUnit": "quoteCoin", "qty": "25", "isLeverage": 0,
            "orderLinkId": "wire-client-identity",
        }])

    def test_ccxt_deterministic_rejection_is_rejected_and_timeout_is_ambiguous_without_retry(self):
        rejected = RecordingBybit(ccxt.InvalidOrder("minimum notional"))
        rejected_result = self._service(rejected).execute(intent(), context())
        self.assertEqual((rejected_result.order.state, len(rejected.requests)), (ExecutionState.REJECTED, 1))

        uncertain = RecordingBybit(TimeoutError("lost after possible POST"))
        service = self._service(uncertain)
        uncertain_result = service.execute(intent(), context())
        duplicate = service.execute(intent(), context())
        self.assertEqual((uncertain_result.order.state, len(uncertain.requests)), (ExecutionState.AMBIGUOUS, 1))
        self.assertEqual(duplicate.risk_status, "DUPLICATE_OR_RECONCILIATION_REQUIRED")
        self.assertEqual(len(uncertain.requests), 1)

    def test_ccxt_signs_expected_bybit_v5_request_without_network_io(self):
        recorder = SignedWireRecorder()
        venue = CcxtVenueAdapter("bybit", recorder.exchange, identity_factory=lambda _: "account-1")

        with patch.object(socket.socket, "connect", side_effect=AssertionError("network I/O is forbidden")):
            acknowledgement = venue.submit_once(intent())

        self.assertEqual(acknowledgement.client_order_id, "wire-client-identity")
        self.assertEqual(len(recorder.requests), 1)
        request = recorder.requests[0]
        parsed_url = urlparse(str(request["url"]))
        self.assertEqual((parsed_url.path, parsed_url.query, request["method"]), ("/v5/order/create", "", "POST"))

        body = json.loads(str(request["body"]))
        self.assertEqual(
            {key: body[key] for key in ("category", "symbol", "side", "orderType", "qty", "marketUnit", "isLeverage", "orderLinkId")},
            {
                "category": "spot", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market",
                "qty": "25", "marketUnit": "quoteCoin", "isLeverage": 0,
                "orderLinkId": "wire-client-identity",
            },
        )
        headers = request["headers"]
        assert isinstance(headers, dict)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["X-BAPI-API-KEY"], recorder.api_key)
        self.assertEqual(headers["X-BAPI-TIMESTAMP"], "1700000000000")
        self.assertEqual(headers["X-BAPI-RECV-WINDOW"], "5000")
        self.assertTrue(headers["X-BAPI-SIGN"])
        self.assertNotIn(recorder.secret, repr(request))


class CurrentWriterReachabilityTests(unittest.TestCase):
    def test_checked_in_v2_cli_policy_never_constructs_a_ccxt_writer(self):
        with TemporaryDirectory() as temporary:
            decision = Path(temporary) / "decision.json"
            decision.write_text('{"decision_id":"wire-audit"}', encoding="utf-8")
            with patch.object(CcxtVenueAdapter, "bybit") as factory, redirect_stdout(StringIO()):
                exit_code = main(["execute", "--decision-json", str(decision)])
            self.assertEqual(exit_code, 2)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
