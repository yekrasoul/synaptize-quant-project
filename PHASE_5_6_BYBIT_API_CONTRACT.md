# Phase 5.6 Bybit API Contract

Verification date: 2026-10-07. Sources are the official Bybit V5 documentation.

| Endpoint / field | Expected semantics | Implementation | Fail-closed behavior | Official source |
|---|---|---|---|---|
| `POST /v5/order/create`: `category` | `spot` | `SpotMarketBuyRequest` | No alternate category | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order) |
| `symbol`, `side`, `orderType` | `BTCUSDT`, `Buy`, `Market` | `SpotMarketBuyRequest` | Identity mismatch blocks | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order) |
| `qty`, `marketUnit` | exact USDT quote amount and `quoteCoin` | `SpotMarketBuyRequest.to_payload` | No BTC/percentage sizing | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order) |
| `isLeverage`, `orderFilter` | `0`, `Order` | payload construction | Margin/conditional path blocked | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order) |
| `orderLinkId` | unique deterministic client identity | `client_order_id` / reconciler | Missing or mismatched identity blocks | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order) |
| `GET /v5/account/info` | complete account context | `BybitPrivateReadClient.account_info` | Missing fields unavailable | [Account Info](https://bybit-exchange.github.io/docs/v5/account/account-info) |
| `GET /v5/account/wallet-balance` | wallet/liability evidence | `wallet_balances` | No balance arithmetic fallback | [Wallet Balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance) |
| Spot quote-buy availability | current contract does not expose the required exact field in this adapter | `WalletBalance.available_for_spot_quote_buy` | Production readiness is `NOT_READY` | [Wallet Balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance) |
| `GET /v5/market/instruments-info` | BTCUSDT Spot rules/status | `instrument_rules` | Malformed/invalid rules block | [Instrument Info](https://bybit-exchange.github.io/docs/v5/market/instrument) |
| `GET /v5/order/realtime` and history | order state bound to `orderLinkId` | `lookup_order` | Ambiguous identity blocks | [Open & Closed Orders](https://bybit-exchange.github.io/docs/v5/order/open-order) |
| `GET /v5/execution/list` | authoritative fills, `execId`, `orderId`, `orderLinkId`, `feeCurrency` | `executions` / reconciler | Missing fee currency or identity blocks | [Execution](https://bybit-exchange.github.io/docs/v5/order/execution) |

The wallet documentation explicitly marks Unified `availableToWithdraw` as deprecated and describes Spot margin availability separately; therefore `walletBalance - locked` is not treated as authoritative Spot quote-buy availability.
