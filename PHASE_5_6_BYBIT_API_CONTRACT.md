# Phase 5.6 Bybit API Contract

Verification date: 2026-10-07. Sources are the official Bybit V5 documentation.

| Endpoint / field | HTTP method | Unit | Expected semantics | Supported account mode | Implementation | Readiness use | Fail-closed behavior | Official source |
|---|---|---|---|---|---|---|---|---|
| `/v5/order/create`: `category` | POST | enum | `spot` | future approved live path only | `SpotMarketBuyRequest` | order identity | No alternate category | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order) |
| `symbol`, `side`, `orderType` | POST | enum | `BTCUSDT`, `Buy`, `Market` | future approved live path only | `SpotMarketBuyRequest` | strategy/instrument identity | Identity mismatch blocks | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order) |
| `qty`, `marketUnit` | POST | USDT / `quoteCoin` | exact quote amount; market Buy uses `quoteCoin` | future approved live path only | `SpotMarketBuyRequest.to_payload` | V1 payload contract | No BTC/percentage sizing | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order) |
| `isLeverage`, `orderFilter` | POST | enum/int | `0`, `Order` | future approved live path only | payload construction | no margin/conditional path | Margin/conditional path blocked | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order) |
| `orderLinkId` | POST | client string | unique deterministic client identity | future approved live path only | `client_order_id` / reconciler | exact reconciliation anchor | Missing/mismatched identity blocks | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order) |
| `unifiedMarginStatus`, `marginMode`, `spotHedgingStatus`, `updatedTime` | GET | enum / UTC freshness | only Unified status `6`, `REGULAR_MARGIN`, `OFF`, fresh timestamp | Unified status 6 | `AccountInfo` / `classify_production_account_mode` | account compatibility | Unknown, stale, or contradictory values fail | [Account Info](https://bybit-exchange.github.io/docs/v5/account/account-info) |
| wallet/liability fields | GET | account balances | BTC/USDT liabilities and interest absent | Unified | `wallet_balances` | wallet/liability gate | No `walletBalance - locked` arithmetic | [Wallet Balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance) |
| `/v5/order/spot-borrow-check`: `spotMaxTradeAmount` | GET | USDT quote amount | actual Spot quote amount available without borrowable amount | Unified Spot | `BybitPrivateReadClient.spot_quote_availability` | authoritative availability and connectivity | Missing/malformed/stale provenance blocks | [Spot Borrow Quota](https://bybit-exchange.github.io/docs/v5/order/spot-borrow-quota) |
| Spot quote-buy availability provenance | GET | USDT | `spotMaxTradeAmount` is actual available quote amount for Spot trading without borrowable amount | Unified status 6 only | `BybitPrivateReadClient.spot_quote_availability` / `availability.py::validate_spot_quote_availability` | required buying-power proof | Plain Decimal, derived, deprecated, generic, stale, or unknown source fails | [Spot Borrow Quota](https://bybit-exchange.github.io/docs/v5/order/spot-borrow-quota) |
| `lotSizeFilter.minOrderAmt` | GET | USDT | minimum quote amount | Spot | `InstrumentRules.quote_minimum` | validates `$10` floor | Missing/malformed minimum blocks | [Instrument Info](https://bybit-exchange.github.io/docs/v5/market/instrument) |
| `lotSizeFilter.maxMarketOrderQty` | GET | base coin quantity | maximum market quantity, not a USDT quote maximum | Spot | `InstrumentRules.max_market_order_qty` | informational only for quoteCoin readiness | Never compare directly to USDT amounts | [Instrument Info](https://bybit-exchange.github.io/docs/v5/market/instrument) |
| QuoteCoin market-buy upper bound | GET | USDT | no authoritative quote-unit upper-bound field is currently implemented | Spot | `QuoteUnitLimitEvidence` / `quote_limits.py`; production policy is empty | proves `$10/$25/$50/$75/$100` only when supplied by an approved source | Upper bound unproven => `UNAVAILABLE` / `NOT_READY`; no base-quantity price conversion | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order), [Instrument Info](https://bybit-exchange.github.io/docs/v5/market/instrument), [Account Instrument Info](https://bybit-exchange.github.io/docs/v5/account/instrument) |
| `/v5/market/time` `timeNano`/`timeSecond` | GET | ns/s converted to ms | authoritative Bybit server clock | public | `BybitPrivateReadClient.server_time_ms` | midpoint clock-skew measurement | Malformed/unavailable time is unavailable | [Server Time](https://bybit-exchange.github.io/docs/v5/market/time) |
| `/v5/market/instruments-info` | GET | Spot metadata | BTCUSDT, BTC/USDT, Trading, precision and limits | public Spot | `instrument_rules` | instrument readiness | Malformed/invalid rules block | [Instrument Info](https://bybit-exchange.github.io/docs/v5/market/instrument) |
| `/v5/order/realtime` and `/v5/order/history` | GET | order identity | order state bound to exact `orderLinkId` | Unified Spot | `lookup_order` | connectivity and recovery | Ambiguous identity blocks | [Open & Closed Orders](https://bybit-exchange.github.io/docs/v5/order/open-order) |
| `/v5/execution/list` | GET | fill quantities/values/fees | authoritative `execId`, `orderId`, `orderLinkId`, `feeCurrency` | Unified Spot | `executions` / reconciler | connectivity and final fill proof | Missing fee currency or identity blocks | [Execution](https://bybit-exchange.github.io/docs/v5/order/execution) |

## Availability provenance policy

Authoritative exact Spot quote-buy availability source currently implemented:
`GET /v5/order/spot-borrow-check`, field `spotMaxTradeAmount`. The official contract
describes this as the actual quote amount available for Spot trading when borrowable
amount is excluded. It is requested for `category=spot`, `symbol=BTCUSDT`, `side=Buy`
and is passed through the shared provenance validator.

`availableToWithdraw` is explicitly rejected because the official contract marks it
deprecated for Unified accounts. A generic `availableBalance` is also rejected unless a
future review proves the exact endpoint, field semantics, account type, and operation
(Unified Spot BTCUSDT Market Buy with `marketUnit=quoteCoin` and `isLeverage=0`).
`walletBalance - locked`, `totalAvailableBalance`, `usdValue`, portfolio totals, and any
other inferred arithmetic are not authoritative substitutes.

Every source approval must be represented by the shared validator
`btc_dca_bridge.availability.validate_spot_quote_availability`, which is used by
production readiness, canary preparation, and live fresh-private checks. The validator
requires an explicit `SpotQuoteAvailability` provenance record, exact approved endpoint
and field, supported account type, `authoritative=true`, UTC observation time, maximum
age of 60 seconds, and no more than 5 seconds of future tolerance. Invalid or unavailable
provenance blocks before any live submission transport can be invoked.

## Phase 5.8 quote-unit limit policy

The official Create Order, public Instruments Info, and Account Instruments Info
contracts document Spot `marketUnit=quoteCoin` but do not expose an authoritative
USDT maximum for the exact BTCUSDT Market Buy operation. `maxOrderAmt` is deprecated,
and `maxMarketOrderQty` is not treated as a quote-unit value. The production policy
`APPROVED_QUOTE_UNIT_LIMIT_SOURCES` is therefore empty and
`market_buy_quote_maximum` remains `None`. `/v5/order/pre-check` is a POST focused on
UTA IMR/MMR calculations and is not used as quote-limit evidence.
