# Phase 5.7 Spot Availability and Instrument-Limit Research

Verification date: 2026-10-07. Primary sources are official Bybit V5 documentation.

## Exact Spot quote-buy availability

| Endpoint | Field | Account type | Unit | Exact operation applicability | Authoritative? | Reason | Official source |
|---|---|---|---|---|---|---|---|
| `GET /v5/account/wallet-balance` | `availableToWithdraw` | `UNIFIED` | balance | No | No | Officially deprecated for Unified accounts; not an order-specific Spot quote-buy field | [Wallet Balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance) |
| `GET /v5/account/wallet-balance` | `totalAvailableBalance` | `UNIFIED` | USD account-wide value | No | No | Account-wide USD margin calculation, not exact USDT quote-buy availability | [Wallet Balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance) |
| `GET /v5/account/wallet-balance` | `walletBalance` / `locked` arithmetic | `UNIFIED` | coin balance | No | No | Inferred arithmetic is not an authoritative order-specific quote amount; `spotBorrow` and account mode semantics matter | [Wallet Balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance) |
| `GET /v5/order/spot-borrow-check` | `spotMaxTradeAmount` | Unified Spot | USDT quote amount | Yes | Yes | Officially documented as actual quote amount available for Spot trading without borrowable amount; query binds Spot, BTCUSDT, Buy | [Get Borrow Quota (Spot)](https://bybit-exchange.github.io/docs/v5/order/spot-borrow-quota) |

Conclusion:

```text
OFFICIAL_SOURCE_CONFIRMED
```

The implementation uses only `spotMaxTradeAmount` from the exact read-only endpoint.
It is represented as `SpotQuoteAvailability` and validated by the shared
`validate_spot_quote_availability` policy. No wallet arithmetic or deprecated field is
used. The value remains subject to the 60-second freshness and 5-second future tolerance
policy.

## Quote-unit market-buy upper bound

The official Spot instrument response exposes `maxMarketOrderQty` as a quantity field,
while the Place Order contract says `marketUnit=quoteCoin` makes `qty` a quote-currency
amount. The current official instrument contract does not expose a clearly named,
authoritative quote-unit maximum for this exact operation. Converting the base quantity
maximum with a current ticker price would introduce undocumented price/slippage semantics.

Conclusion:

```text
QUOTE_MAX_NOT_EXPOSED
```

`market_buy_quote_maximum` therefore remains `None`, and Phase 5.6 readiness remains
blocked on this independent instrument proof even though Spot availability is now sourced
from the official endpoint.

Sources: [Get Instruments Info](https://bybit-exchange.github.io/docs/v5/market/instrument),
[Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order).
