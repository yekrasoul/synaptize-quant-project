# Production Operations Runbook

## Daily observe-only check

Run these read-only commands:

```bash
btc-dca production-status --json
btc-dca production-blockers --json
btc-dca contract-status --json
btc-dca preauthorization-status --json
```

These commands report the legacy observe-only operations model and may not
reflect current V2 activation blockers. Do **not** interpret old
`BYBIT_QUOTE_UNIT_MAX_NOT_EXPOSED` results as the V2 readiness decision:
the current quote-limit policy does not require an independently exposed
quote-unit maximum. Confirm actual output against
[`v2_activation_checklist.yaml`](btc_dca_bridge/config/v2_activation_checklist.yaml)
and [`runtime_execution_policy.yaml`](btc_dca_bridge/config/runtime_execution_policy.yaml).
V2 is presently `BLOCKED`, execution is disabled, and the kill switch is
active. Account/environment identity, credential withdrawal exclusion and
operational Shadow evidence remain incomplete; no execution authorization is
implied. The only gated CLI writer route is `btc-dca execute`;
`canary-execute` is permanently retired and fail-closed.

## Immutable checkpoint

Capture an append-only status snapshot when an operational checkpoint is
needed:

```bash
btc-dca collect-production-status --json
```

Snapshots are status history, not spend or authorization records. Alert
artifacts are derived from adjacent snapshots and can be reconstructed if a
process stops after snapshot publication.

## History

```bash
btc-dca production-status-history --limit 20 --json
```

## Reconciliation

Unresolved operations take precedence over the external contract blocker. If
any prior submission may have reached Bybit, do not prepare or submit a new
order for that identity. Never retry or top up. Use only the existing
`reconcile-existing` recovery path for the exact prior identity; it is a
no-POST recovery operation. Do not edit the canonical ledger manually.

## Quote-limit policy and historical workflow

The old Phase 5 guidance treated `BYBIT_QUOTE_UNIT_MAX_NOT_EXPOSED` as a
required external blocker and described a future `canary-execute`
authorization sequence. That sequence is **historical and not an active
production activation procedure**. The current
[`QuoteUnitLimitPolicy`](btc_dca_bridge/src/btc_dca_bridge/quote_limits.py)
has `quote_unit_maximum_required=False`; no base-quantity × price estimate
or substitute quote ceiling is approved. The exchange may still reject a
submitted quote-sized order; no retry/top-up is implied.

For current V2 execution readiness, use the activation checklist, runtime
policy, [V2 cutover](BYBIT_CCXT_CUTOVER.md), and
[Execution V2](EXECUTION_V2.md) together. All independent readiness and
account/credential gates must be satisfied through authorized operational
evidence before any activation; this runbook neither authorizes nor
initiates an order.

# Quote-sized order constraint

The live payload is an exact Bybit Spot BTCUSDT Market Buy with
`marketUnit=quoteCoin`; its `qty` is the approved USDT amount. Do not derive a
quote ceiling from `maxMarketOrderQty`, multiply by market price, use wallet
balances as a fake maximum, or revive deprecated `maxOrderAmt`. Current
non-borrowed Spot availability from
`/v5/order/spot-borrow-check:spotMaxTradeAmount` remains mandatory, as do the
monthly cap, minimum amount, identity, approval, freshness, liability,
duplicate-submission, reconciliation, and exactly-once ledger controls.

If Bybit rejects the request for an exchange-side limit, record
`exchange_rejected`/`rejected_by_exchange`, do not retry or top up, assume no
fill, and do not append the ledger. This is not an acceptance guarantee.
