# Phase 5.8 Quote-Unit Limit Research

Verification date: 2026-10-07. Primary sources are official Bybit V5 documentation.

| Endpoint | Method | Field | Unit | Exact Spot `quoteCoin` applicability | Authoritative quote maximum? | Suitability |
|---|---|---|---|---|---|---|
| `/v5/order/create` | POST | `qty`, `marketUnit` | quote currency when `marketUnit=quoteCoin` | Yes | No maximum is exposed; response is only asynchronous acceptance | Not a limit source |
| `/v5/market/instruments-info` | GET | `lotSizeFilter.maxMarketOrderQty` | market-order quantity; no documented quote-value semantics | Yes | No | Cannot prove a USDT quote maximum |
| `/v5/market/instruments-info` | GET | `lotSizeFilter.maxOrderAmt` | deprecated amount field | Historical Spot field | No; official contract says no longer check it | Rejected |
| `/v5/account/instruments-info` | GET | `lotSizeFilter.maxMarketOrderQty` | market-order quantity; no quote-unit semantics | Yes for eligible accounts | No | No quote ceiling |
| `/v5/order/pre-check` | POST | `preImrE4`, `postImrE4`, `preMmrE4`, `postMmrE4` | margin rates | Not a documented Spot quote-limit proof | No | `PRE_CHECK_NOT_SUITABLE_FOR_QUOTE_LIMIT_PROOF`; not called |
| `/v5/market/order-price-limit` | GET | price-limit fields | price protection | Not a quote amount limit | No | Not applicable |

Create Order documents Spot Market Buy `marketUnit=quoteCoin`, `isLeverage=0`, and
`orderFilter=Order`, but exposes no quote-denominated upper bound. No base-quantity ×
ticker/ask/mark/index conversion is used.

## Conclusion

```text
QUOTE_UNIT_MAX_NOT_EXPOSED
```

`market_buy_quote_maximum` remains `None` in production. The production approved-source
set is empty. Test-only injected policies exercise validator mechanics only.

Official sources:

- https://bybit-exchange.github.io/docs/v5/order/create-order
- https://bybit-exchange.github.io/docs/v5/market/instrument
- https://bybit-exchange.github.io/docs/v5/account/instrument
- https://bybit-exchange.github.io/docs/v5/order/pre-check-order
- https://bybit-exchange.github.io/docs/v5/market/order-price-limit
# Reviewed policy transition: quote-sized Spot Market Buy

For `category=spot`, `symbol=BTCUSDT`, `side=Buy`, `orderType=Market`, and
`marketUnit=quoteCoin`, Bybit interprets `qty` as the exact submitted USDT
quote amount. The deprecated Spot `maxOrderAmt` is no longer checked. The
current `lotSizeFilter.maxMarketOrderQty` is quantity/base-side metadata and
must not be multiplied by a ticker price or treated as a quote-USDT ceiling.

Accordingly, an independently exposed quote maximum is not required for this
exact operation. An order that violates an exchange-side quantity or risk
limit may be explicitly rejected; that rejection is fail-safe, is never
retried, assumes no fill, and appends no ledger execution. This does not claim
that Bybit guarantees acceptance.

The required controls remain the exact Decision, OrderIntent, LiveApproval,
monthly cap, instrument minimum, authoritative non-borrowed
`/v5/order/spot-borrow-check:spotMaxTradeAmount`, no liabilities, exact
Bybit identity/payload, duplicate protection, fresh evidence, reconciliation,
and exactly-once append from authoritative fills only.
