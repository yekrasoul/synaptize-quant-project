# Phase 5.9 — Blocked Production Mode

This phase documents a fail-closed, observe-only production posture. The
absence of an independently exposed quote-denominated maximum is not itself
the reason for the posture and is not an active production blocker for the
approved quote-sized Spot Market Buy.

## Authoritative contract state

For the exact Bybit operation:

- `category=spot`
- `symbol=BTCUSDT`
- `side=Buy`
- `orderType=Market`
- `marketUnit=quoteCoin`

the payload `qty` is the exact approved USDT amount. The reviewed capability
state is:

```text
quote_unit_maximum_supported = false
quote_unit_maximum_required = false
production_quote_limit_conclusion = QUOTE_UNIT_MAX_NOT_REQUIRED
real_money_authorization = NOT_AUTHORIZED
```

`BYBIT_QUOTE_UNIT_MAX_NOT_EXPOSED` is not an active production blocker under
this approved quote-sized operation. `production-blockers` must not report it
solely because independent quote maximum evidence is absent. The capability
state remains truthful: no fake maximum is fabricated and no
`QUOTE_UNIT_MAX_CONFIRMED` conclusion is emitted.

`maxOrderAmt` remains deprecated and unused. `maxMarketOrderQty` remains
authoritative Bybit quantity/base-side metadata only; it is never multiplied
by market price and never converted into a USDT ceiling. No ticker-derived
limit, wallet-derived limit, deprecated field, or state-changing pre-check
POST is used.

## Contract research verification — 2026-10-10

The reviewed Bybit Spot order contract supports Market Buy by quote value
through `marketUnit=quoteCoin`. The Spot instrument contract exposes
`maxMarketOrderQty` as a maximum order quantity and identifies `maxOrderAmt`
as deprecated. Therefore the absence of a separately exposed quote maximum is
an exchange-validity consideration, not an overspend path.

The account-scoped, non-borrowed Spot availability check remains mandatory:
`/v5/order/spot-borrow-check:spotMaxTradeAmount`. The intended quote amount
must remain within that authoritative current availability, as well as the
monthly cap and instrument minimum.

If an exact quote-sized order exceeds an exchange-side quantity or risk limit,
the expected safe result is an explicit exchange rejection. It is never
retried or topped up, no fill is assumed, no BTC purchase is claimed, and no
ledger row is appended. This policy does not guarantee that Bybit accepts the
order.

## Remaining safety boundaries

The following controls remain required before any possible submission:

- exact Decision `final_purchase_usd`, OrderIntent amount, and LiveApproval
  identity/amount;
- current calendar-month spend and the hard `$500` cap;
- authoritative non-borrowed `spotMaxTradeAmount` availability;
- no liabilities or borrowing;
- exact Bybit Spot BTCUSDT identity and exact quote-denominated payload;
- duplicate and ambiguous-submission protection;
- fresh approval, account, instrument, and availability evidence;
- post-ACK reconciliation; and
- exactly-once ledger append from authoritative fills only.

Checked-in production safety remains disabled:

```text
live_execution_enabled = false
kill_switch = true
order_submission = not_implemented
real_money_authorization = NOT_AUTHORIZED
```

Consequently, this document describes a blocked/observe-only operational mode,
not authorization or activation of real-money execution. Other independent
safety, freshness, liability, availability, configuration, or reconciliation
failures remain blocking when present.

## Future contract-change boundary

A future Bybit documentation or API change does not automatically become
trusted. Any future capability change still requires official-source research,
manual semantic review, explicit validator/policy changes, offline tests,
reviewed merge, fresh evidence, and separate real-money authorization.

Capability comparison is informational only. A capability change never grants
authorization or changes the checked-in execution defaults.

NO REAL ORDER EXECUTED
LIVE TRADING NOT ACTIVATED
REAL-MONEY AUTHORIZATION REMAINS NOT_AUTHORIZED
