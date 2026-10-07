# Phase 5.4A — Controlled Canary Preparation

**PHASE 5.4A DOES NOT EXECUTE A REAL ORDER.**

This phase prepares one immutable, short-lived package for human review. It
does not call the Phase 5.3 submission transport and cannot send
`POST /v5/order/create`. The checked-in production configuration remains:

```text
live_execution_enabled: false
kill_switch: true
order_submission: not_implemented
```

## Two-stage process

`canary-prepare` binds one V1 Decision, its deterministic OrderIntent and
client order ID, the current UTC calendar month, fresh canonical-ledger state,
private Bybit verification, wallet/liability checks, instrument metadata, and
fresh pre-submission reconciliation. It creates a `CanaryManifest` with
status `READY_FOR_MANUAL_APPROVAL` only when every preparation gate passes.
Otherwise it creates a `BLOCKED` manifest with explicit reasons.

The manifest is the boundary between preparation and a future Phase 5.4B
manual execution step. It expires after 15 minutes and cannot be extended or
used as a standing approval. `CanaryExecutor` is an explicit disabled
placeholder and raises `CANARY EXECUTION NOT ENABLED`.

## Safety semantics

The amount is exactly the approved V1 `final_purchase_usd` amount. No `$10`
test amount, reduction, increase, split, rounding-up, or wallet-driven
adjustment is allowed. The future request is constructed in memory as the
single supported shape: Bybit Spot BTCUSDT Buy Market, exact USDT quote amount,
`marketUnit=quoteCoin`, `isLeverage=0`, and `orderFilter=Order`. Its canonical
JSON receives an immutable SHA-256 fingerprint.

Preparation rereads the canonical ledger and rejects cap overflow, prior
confirmed evidence, stale month context, and any existing submission attempt
or outcome for the same deterministic order identity. It requires a
`TRADE_CAPABLE` Spot-only credential, valid account context, sufficient
immediately available USDT, BTC/USDT with no borrow or accrued interest, valid
authoritative BTCUSDT Spot metadata, and fresh Phase 5.2 reconciliation that
is exactly `conclusively_absent`. Any active, partial, confirmed, ambiguous,
failed, or contradictory read blocks preparation.

No API key, API secret, signature, authorization header, or raw credential is
included in the manifest or CLI output. No Execution, fill, acknowledgement,
or ledger record is created. The manifest is persisted append-only with a
SHA-256 sidecar under `canary_manifests/`.

## Future phase

Phase 5.4B must separately introduce a narrowly scoped one-shot activation
mechanism and revalidate the still-fresh manifest. Phase 5.4A itself never
submits, schedules, retries, or enables live trading.
