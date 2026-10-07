# Phase 5.3 — Live Order Engine, Disabled by Default

Phase 5.3 contains a narrowly scoped future-capable Bybit order engine, but
the checked-in production configuration remains disabled:

```text
live_execution_enabled: false
kill_switch: true
order_submission: not_implemented
```

**LIVE TRADING IS NOT ACTIVATED BY PHASE 5.3**

## Supported shape

The only request model is a Bybit Spot BTCUSDT Market Buy using the exact V1
USDT quote amount:

```text
category=spot, symbol=BTCUSDT, side=Buy, orderType=Market
marketUnit=quoteCoin, isLeverage=0, orderFilter=Order
```

The `qty` field is the approved whole-dollar USDT amount. It is never
translated to BTC, rounded upward, or silently adjusted. No generic order
builder exists. Sell, Limit, derivatives, leverage, margin, TP/SL, transfer,
withdrawal, borrow, repay, amend, cancel, and batch operations are not
supported.

## Safety and recovery

Before the one possible POST, the engine requires fresh Phase 5.1 safety,
current instrument metadata, a trade-capable Spot-only credential, an exact
deterministic `orderLinkId`, conclusive absence from Phase 5.2 reads, and a
short-lived injected `LiveApproval` bound to the immutable intent and amount.
Production cannot supply this approval because the production gates remain
disabled.

The only executable POST endpoint is `/v5/order/create`. It is exposed through
`submit_spot_market_buy()` only; there is no arbitrary POST method. A POST is
never blindly retried. Timeout, transport failure after the request boundary,
5xx, malformed acknowledgements, or identity mismatch are ambiguous and
require read-only reconciliation by `orderLinkId`.

An ACK is not a fill. After an ACK, the engine performs a new Phase 5.2
read-back; it never reuses the pre-submit absence result. Active, partial,
empty, contradictory, or failed read-backs remain ambiguous. A fresh confirmed
fill is recorded as reconciliation evidence only; the outcome artifact uses
`ledger_not_mutated: true` to state precisely that no canonical ledger write
occurred. Submission attempts and outcomes are immutable checksum-backed
artifacts. Only a future explicit reconciliation operation could justify a
confirmed execution record.

The `live-submit` CLI is a production safety stop and reports:

```text
LIVE EXECUTION DISABLED
NO ORDER SUBMITTED
```

No scheduled workflow invokes it. Phase 4 production shadow behavior is
unchanged. Phase 5.4 is required before any controlled real-money canary.
