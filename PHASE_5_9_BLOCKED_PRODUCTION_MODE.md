# Phase 5.9 — Blocked Production Mode

The current production state is intentionally fail-closed:

- `QUOTE_UNIT_MAX_NOT_EXPOSED`
- production readiness: `NOT_READY`
- preauthorization: `BLOCKED`
- real-money authorization: `NOT_AUTHORIZED`

This is a healthy software state with an external Bybit contract dependency. It
is reported as `PRODUCTION_BLOCKED_EXTERNAL_CONTRACT` and as
`HEALTHY_BLOCKED_EXTERNAL_DEPENDENCY` by operational health. It is not a
software defect and it does not authorize or enable execution.

The reviewed contract exposes `spotMaxTradeAmount` through the read-only Spot
borrow-check path for account availability, but does not expose an authoritative
quote-denominated maximum for the exact BTCUSDT Spot Market Buy using
`marketUnit=quoteCoin`. Base-quantity maximums cannot be multiplied by a ticker
price: execution price and slippage semantics would be invented. The Pre Check
Order POST is not used, and no order-create probe is used.

The operator commands `production-blockers` and `contract-status` are read-only.
They report the active `MARKET_CONTRACT` blocker, current capability snapshot,
and the immutable authorization state. The blocker clears only when both an
explicitly approved source policy and fresh `QuoteUnitLimitEvidence` pass the
shared quote-limit validator. Policy presence alone and evidence alone are
insufficient. The current Bybit provider reports `NOT_EXPOSED`, and the
production approved-source set remains empty.

## Future contract-change boundary

A future Bybit documentation or API change does not automatically become
trusted. The required sequence is:

1. discover an official source;
2. manually research its semantics;
3. review the endpoint and field for the exact operation;
4. update the shared validator;
5. update the production policy explicitly;
6. pass offline tests;
7. complete PR review;
8. recollect production evidence;
9. reevaluate readiness;
10. obtain separate real-money authorization.

Capability comparison is informational only. A `CAPABILITY_ADDED` result never
changes `APPROVED_QUOTE_UNIT_LIMIT_SOURCES` automatically.

## Operator interpretation

Health checks integrity first, then execution configuration, unresolved
submission/reconciliation state, and finally external contract blockers. If
reconciliation is unresolved, health reports
`HEALTHY_WITH_UNRESOLVED_RECONCILIATION` and may include the external blocker
as supplemental context. `HEALTHY_BLOCKED_EXTERNAL_DEPENDENCY` means internal
operational state is otherwise healthy while an external contract prerequisite
is missing. `CORRUPT` means software evidence cannot be trusted. These states
permit observation and recovery only; none permits an order.

`live_execution_enabled=false`, `kill_switch=true`, and
`order_submission=not_implemented` remain checked-in production defaults.

NO REAL ORDER EXECUTED
LIVE TRADING NOT ACTIVATED
REAL-MONEY CANARY STILL REQUIRES SEPARATE EXPLICIT USER AUTHORIZATION
