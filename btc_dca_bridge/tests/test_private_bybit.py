import hashlib
import hmac
import json
import unittest
from decimal import Decimal
from decimal import Decimal

from btc_dca_bridge.market_data.http import HttpResponse
from btc_dca_bridge.private_bybit import (
    ACCOUNT_INFO, EXECUTION_LIST, INSTRUMENTS_INFO, ORDER_HISTORY, ORDER_REALTIME, SERVER_TIME,
    WALLET_BALANCE, SPOT_BORROW_CHECK, ApiCredentialInfo, AuthenticationError, BybitPostAckReconciler, BybitPrivateReadClient,
    CredentialClassification, ExecutionFill, MalformedBybitResponseError, OrderState, PermissionError, ReadOnlyOrder,
    canonical_query, signature,
)


def response(result, *, code=0, status=200):
    return HttpResponse(status, {}, json.dumps({"retCode": code, "result": result}).encode())


def instrument_result():
    return {"category": "spot", "list": [{"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading", "lotSizeFilter": {"minOrderAmt": "10", "minOrderQty": "0.00001", "basePrecision": "0.000001", "quotePrecision": "0.01", "qtyStep": "0.00001"}, "priceFilter": {"tickSize": "0.01"}}]}


class FakeTransport:
    def __init__(self, responses): self.responses, self.calls = responses, []
    def get(self, path, params, headers):
        self.calls.append((path, dict(params), dict(headers)))
        value = self.responses.get(path)
        if callable(value): return value(dict(params))
        return value or response({})


class PrivateBybitTests(unittest.TestCase):
    def client(self, responses): return BybitPrivateReadClient(FakeTransport(responses))

    def test_signature_and_query_are_deterministic(self):
        query = canonical_query({"symbol": "BTCUSDT", "category": "spot"})
        self.assertEqual(query, "category=spot&symbol=BTCUSDT")
        expected = hmac.new(b"secret", b"123key5000category=spot&symbol=BTCUSDT", hashlib.sha256).hexdigest()
        self.assertEqual(signature("123", "key", "5000", query, "secret"), expected)

    def test_credential_classification_and_secret_not_in_model(self):
        payload = {"id": "user1", "readOnly": 1, "permission": {"Spot": ["SpotRead"]}, "apiKey": "secret-key", "secret": "secret-value", "ips": ["1.2.3.4"]}
        client = self.client({"/v5/user/query-api": response(payload)})
        info = client.credential_info()
        self.assertEqual(info.classification, CredentialClassification.READ_ONLY)
        self.assertNotIn("secret-key", repr(info)); self.assertNotIn("secret-value", repr(info))
        trade = self.client({"/v5/user/query-api": response({"readOnly": 0, "permission": {"Spot": ["SpotTrade"]}})}).credential_info()
        self.assertEqual(trade.classification, CredentialClassification.TRADE_CAPABLE)
        unsafe = self.client({"/v5/user/query-api": response({"readOnly": 0, "permission": {"Wallet": ["AccountTransfer"]}})}).credential_info()
        self.assertEqual(unsafe.classification, CredentialClassification.UNSAFE_PERMISSION_SCOPE)

        for permission in (("ContractTrade", "Order"), ("ContractTrade", "Position"), ("Options", "OptionsTrade"), ("Wallet", "Withdraw"), ("Wallet", "SubMemberTransfer"), ("Wallet", "Borrow")):
            group, action = permission
            classified = self.client({"/v5/user/query-api": response({"readOnly": 0, "permission": {group: [action]}})}).credential_info()
            self.assertEqual(classified.classification, CredentialClassification.UNSAFE_PERMISSION_SCOPE, permission)
        malformed = self.client({"/v5/user/query-api": response({"readOnly": 0, "permission": {"UnknownGroup": ["UnknownAction"]}})}).credential_info()
        self.assertEqual(malformed.classification, CredentialClassification.INVALID)

    def test_spot_trade_with_unrelated_empty_bybit_groups_is_trade_capable(self):
        permissions = {"Spot": ["SpotTrade"], "Affiliate": [], "BitCard": [], "BlockTrade": [], "CopyTrading": [], "Earn": [], "Exchange": [], "Wallet": [], "Unrelated": []}
        info = self.client({"/v5/user/query-api": response({"readOnly": 0, "permission": permissions})}).credential_info()
        self.assertEqual(info.classification, CredentialClassification.TRADE_CAPABLE)

    def test_uta_unified_derivatives_parent_is_tolerated_for_spot_only(self):
        permissions = {"Spot": ["SpotTrade"], "Derivatives": ["DerivativesTrade"], "ContractTrade": [], "Options": [], "Wallet": []}
        info = self.client({"/v5/user/query-api": response({"readOnly": 0, "permission": permissions})}).credential_info()
        self.assertEqual(info.classification, CredentialClassification.TRADE_CAPABLE)

    def test_spot_trade_only_is_trade_capable(self):
        permissions = {"Spot": ["SpotTrade"], "Derivatives": [], "ContractTrade": [], "Options": [], "Wallet": []}
        info = self.client({"/v5/user/query-api": response({"readOnly": 0, "permission": permissions})}).credential_info()
        self.assertEqual(info.classification, CredentialClassification.TRADE_CAPABLE)

    def test_unified_derivatives_parent_without_spot_fails_closed(self):
        permissions = {"Spot": [], "Derivatives": ["DerivativesTrade"], "ContractTrade": [], "Options": [], "Wallet": []}
        info = self.client({"/v5/user/query-api": response({"readOnly": 0, "permission": permissions})}).credential_info()
        self.assertEqual(info.classification, CredentialClassification.INVALID)

    def test_uta_parent_shape_preserves_contract_options_wallet_and_unknown_guards(self):
        cases = (
            ({"Spot": ["SpotTrade"], "ContractTrade": ["Order"]}, CredentialClassification.UNSAFE_PERMISSION_SCOPE),
            ({"Spot": ["SpotTrade"], "ContractTrade": ["Position"]}, CredentialClassification.UNSAFE_PERMISSION_SCOPE),
            ({"Spot": ["SpotTrade"], "Options": ["OptionsTrade"]}, CredentialClassification.UNSAFE_PERMISSION_SCOPE),
            ({"Spot": ["SpotTrade"], "Wallet": ["AccountTransfer"]}, CredentialClassification.UNSAFE_PERMISSION_SCOPE),
            ({"Spot": ["SpotTrade"], "Derivatives": ["DerivativesTrade"], "UnknownGroup": ["UnknownAction"]}, CredentialClassification.INVALID),
        )
        for permissions, expected in cases:
            with self.subTest(permissions=permissions):
                info = self.client({"/v5/user/query-api": response({"readOnly": 0, "permission": permissions})}).credential_info()
                self.assertEqual(info.classification, expected)

    def test_read_only_uta_spot_trade_is_invalid(self):
        permissions = {"Spot": ["SpotTrade"], "Derivatives": ["DerivativesTrade"], "ContractTrade": [], "Options": [], "Wallet": []}
        info = self.client({"/v5/user/query-api": response({"readOnly": 1, "permission": permissions})}).credential_info()
        self.assertEqual(info.classification, CredentialClassification.INVALID)

    def test_safe_read_actions_with_empty_groups_are_read_only(self):
        permissions = {"Spot": ["SpotRead"], "Wallet": ["WalletRead"], "Affiliate": [], "Exchange": [], "UnknownEmpty": []}
        info = self.client({"/v5/user/query-api": response({"readOnly": 1, "permission": permissions})}).credential_info()
        self.assertEqual(info.classification, CredentialClassification.READ_ONLY)

    def test_non_empty_unknown_group_and_read_only_trade_fail_closed(self):
        unknown = self.client({"/v5/user/query-api": response({"readOnly": 0, "permission": {"UnknownGroup": ["UnknownAction"]}})}).credential_info()
        self.assertEqual(unknown.classification, CredentialClassification.INVALID)
        read_only_trade = self.client({"/v5/user/query-api": response({"readOnly": 1, "permission": {"Spot": ["SpotTrade"]}})}).credential_info()
        self.assertEqual(read_only_trade.classification, CredentialClassification.INVALID)

    def test_account_and_wallet_parse_decimal_liabilities(self):
        client = self.client({
            "/v5/account/info": response({"unifiedMarginStatus": 6, "marginMode": "REGULAR_MARGIN", "spotHedgingStatus": "ON", "updatedTime": "1"}),
            "/v5/account/wallet-balance": response({"list": [{"coin": [{"coin": "BTC", "walletBalance": "0.1", "locked": "0.01", "borrowAmount": "0.02", "accruedInterest": "0.003", "usdValue": "500"}, {"coin": "USDT", "walletBalance": "100", "locked": "2", "borrowAmount": "0", "accruedInterest": "0", "usdValue": "100"}]}]}),
        })
        self.assertEqual(client.account_info().unified_margin_status, 6)
        balances = client.wallet_balances()
        self.assertEqual(balances[0].wallet_balance, Decimal("0.1")); self.assertTrue(balances[0].has_liability)

    def test_spot_quote_availability_uses_authoritative_quote_field(self):
        availability = self.client({SPOT_BORROW_CHECK: response({"symbol": "BTCUSDT", "side": "Buy", "spotMaxTradeAmount": "123.45"})}).spot_quote_availability()
        self.assertEqual(availability.amount_usdt, Decimal("123.45"))
        self.assertEqual((availability.source_endpoint, availability.source_field, availability.account_type), (SPOT_BORROW_CHECK, "spotMaxTradeAmount", "UNIFIED"))
        with self.assertRaises(MalformedBybitResponseError):
            self.client({SPOT_BORROW_CHECK: response({})}).spot_quote_availability()
        for identity in ({"symbol": "ETHUSDT", "side": "Buy"}, {"symbol": "BTCUSDT", "side": "Sell"}, {"side": "Buy"}, {"symbol": "BTCUSDT"}):
            with self.subTest(identity=identity), self.assertRaises(MalformedBybitResponseError):
                self.client({SPOT_BORROW_CHECK: response({**identity, "spotMaxTradeAmount": "123.45"})}).spot_quote_availability()

    def test_instrument_adapter_uses_hardened_parser(self):
        rules = self.client({INSTRUMENTS_INFO: response(instrument_result())}).instrument_rules()
        self.assertEqual(rules.quote_minimum, Decimal("10"))

    def test_production_quote_limit_provider_is_truthfully_not_exposed(self):
        evidence = self.client({}).quote_unit_limit_evidence()
        self.assertEqual(evidence.conclusion, "NOT_EXPOSED")
        self.assertFalse(evidence.authoritative)
        self.assertIsNone(evidence.maximum_quote_usdt)
        self.assertEqual(evidence.unit, "baseCoin")

    def test_server_time_parser_is_get_only_and_strict(self):
        self.assertEqual(self.client({SERVER_TIME: response({"timeSecond": "1791374340"})}).server_time_ms(), 1791374340000)
        self.assertEqual(self.client({SERVER_TIME: response({"timeNano": "1791374340000000000"})}).server_time_ms(), 1791374340000)
        with self.assertRaises(MalformedBybitResponseError):
            self.client({SERVER_TIME: response({"timeSecond": "bad"})}).server_time_ms()

    def test_order_states_and_execution_fills(self):
        order = {"orderLinkId": "dca-abc", "category": "spot", "symbol": "BTCUSDT", "orderId": "oid", "orderStatus": "PartiallyFilled", "cumExecQty": "0.1", "cumExecValue": "10"}
        fill = {"execQty": "0.1", "execPrice": "100", "execValue": "10", "execFee": "0.01", "feeCurrency": "USDT", "execTime": "1", "execId": "eid", "orderId": "oid", "orderLinkId": "dca-abc", "category": "spot", "symbol": "BTCUSDT"}
        client = self.client({ORDER_REALTIME: response({"list": [order]}), ORDER_HISTORY: response({"list": []}), EXECUTION_LIST: response({"list": [fill]})})
        self.assertEqual(client.lookup_order("dca-abc").state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(client.executions("dca-abc")[0].exec_qty, Decimal("0.1"))
        self.assertEqual(client.submission_state("dca-abc"), "confirmed")

    def test_execution_fee_currency_is_authoritative_and_required(self):
        base = {"execQty": "0.1", "execPrice": "100", "execValue": "10", "execFee": "0.01", "execTime": "1", "execId": "eid", "orderId": "oid", "orderLinkId": "dca-abc", "category": "spot", "symbol": "BTCUSDT"}
        client = self.client({EXECUTION_LIST: response({"list": [dict(base, feeCurrency="BTC")]})})
        self.assertEqual(client.executions("dca-abc")[0].fee_asset, "BTC")
        missing = self.client({EXECUTION_LIST: response({"list": [base]})})
        with self.assertRaises(MalformedBybitResponseError): missing.executions("dca-abc")

    def test_post_ack_reconciler_preserves_partial_fill_evidence(self):
        class Client:
            def lookup_order(self, client_order_id): return ReadOnlyOrder(client_order_id, "order-1", OrderState.PARTIALLY_FILLED, "PartiallyFilled", Decimal("0.1"), Decimal("10"))
            def executions(self, client_order_id): return (ExecutionFill(Decimal("0.1"), Decimal("100"), Decimal("10"), Decimal("0.01"), "USDT", "2026-10-07T12:00:01Z", "exec-1", "order-1", client_order_id, "spot", "BTCUSDT"),)
        evidence = BybitPostAckReconciler(Client()).reconcile_after_ack("dca-abc")
        self.assertEqual(evidence.state, "partial"); self.assertEqual(evidence.order_id, "order-1")
        self.assertEqual(evidence.order_link_id, "dca-abc"); self.assertEqual(evidence.fills[0].fee_asset, "USDT")

    def test_absent_is_only_after_both_successful_order_reads_and_execution_read(self):
        client = self.client({ORDER_REALTIME: response({"list": []}), ORDER_HISTORY: response({"list": []}), EXECUTION_LIST: response({"list": []})})
        self.assertEqual(client.submission_state("dca-none"), "conclusively_absent")
        timeout_client = self.client({ORDER_REALTIME: response({}, status=503)})
        self.assertEqual(timeout_client.submission_state("dca-uncertain"), "ambiguous")

    def test_conflicting_order_results_are_ambiguous(self):
        active = {"orderLinkId": "dca-x", "orderStatus": "New", "cumExecQty": "0", "cumExecValue": "0"}
        filled = {"orderLinkId": "dca-x", "orderStatus": "Filled", "cumExecQty": "1", "cumExecValue": "10"}
        client = self.client({ORDER_REALTIME: response({"list": [active]}), ORDER_HISTORY: response({"list": [filled]}), EXECUTION_LIST: response({"list": []})})
        self.assertEqual(client.lookup_order("dca-x").state, OrderState.AMBIGUOUS)
        self.assertEqual(client.submission_state("dca-x"), "ambiguous")

    def test_mismatched_order_identity_fails_closed_for_realtime_and_history(self):
        bad = {"orderLinkId": "dca-other", "category": "spot", "symbol": "BTCUSDT", "orderStatus": "Filled", "cumExecQty": "1", "cumExecValue": "10"}
        realtime = self.client({ORDER_REALTIME: response({"list": [bad]}), ORDER_HISTORY: response({"list": []})})
        with self.assertRaises(MalformedBybitResponseError): realtime.lookup_order("dca-requested")
        history = self.client({ORDER_REALTIME: response({"list": []}), ORDER_HISTORY: response({"list": [bad]})})
        with self.assertRaises(MalformedBybitResponseError): history.lookup_order("dca-requested")

    def test_order_symbol_category_and_fill_identity_are_bound(self):
        bad_order = {"orderLinkId": "dca-id", "category": "linear", "symbol": "BTCUSDT", "orderStatus": "New", "cumExecQty": "0", "cumExecValue": "0"}
        with self.assertRaises(MalformedBybitResponseError): self.client({ORDER_REALTIME: response({"list": [bad_order]}), ORDER_HISTORY: response({"list": []})}).lookup_order("dca-id")
        bad_fill = {"execQty": "0.1", "execPrice": "100", "execValue": "10", "execFee": "0", "feeCurrency": "USDT", "execTime": "1", "execId": "eid", "orderId": "oid", "orderLinkId": "dca-other", "category": "spot", "symbol": "BTCUSDT"}
        client = self.client({EXECUTION_LIST: response({"list": [bad_fill]})})
        with self.assertRaises(MalformedBybitResponseError): client.executions("dca-id")
        self.assertEqual(client.submission_state("dca-id"), "ambiguous")

    def test_conflicting_order_and_fill_ids_are_ambiguous(self):
        order = {"orderLinkId": "dca-id", "orderId": "order-a", "orderStatus": "Filled", "cumExecQty": "1", "cumExecValue": "10"}
        fill = {"execQty": "1", "execPrice": "10", "execValue": "10", "execFee": "0", "feeCurrency": "USDT", "execTime": "1", "execId": "eid", "orderId": "order-b", "orderLinkId": "dca-id"}
        client = self.client({ORDER_REALTIME: response({"list": [order]}), ORDER_HISTORY: response({"list": []}), EXECUTION_LIST: response({"list": [fill]})})
        self.assertEqual(client.submission_state("dca-id"), "ambiguous")

    def test_allowlist_rejects_arbitrary_and_mutating_paths(self):
        client = self.client({})
        with self.assertRaises(PermissionError): client._read("/v5/order/create", {})
        with self.assertRaises(PermissionError): client._read("/v5/transfer/asset-transfer", {})
        self.assertFalse(hasattr(client, "post")); self.assertFalse(hasattr(client, "create_order"))

    def test_malformed_private_responses_fail_closed(self):
        with self.assertRaises(MalformedBybitResponseError): self.client({ACCOUNT_INFO: HttpResponse(200, {}, b"not-json")}).account_info()
        with self.assertRaises(MalformedBybitResponseError): self.client({ACCOUNT_INFO: response({})}).account_info()
        with self.assertRaises(MalformedBybitResponseError): self.client({WALLET_BALANCE: response({"list": [{"coin": [{"coin": "BTC", "walletBalance": "bad"}]}]})}).wallet_balances()


if __name__ == "__main__": unittest.main()
