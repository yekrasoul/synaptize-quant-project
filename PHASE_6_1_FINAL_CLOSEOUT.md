# Phase 6.1 — Final Production Closeout & Handoff

## Authoritative state

```text
software_health = HEALTHY
production_readiness = NOT_READY
preauthorization = BLOCKED
real_money_authorization = NOT_AUTHORIZED
external_blocker = none for quote maximum exposure
quote_unit_maximum_supported = false
quote_unit_maximum_required = false
live_execution_enabled = false
kill_switch = true
order_submission = not_implemented
```

The absence of an independently exposed quote-denominated maximum is not an
active production blocker for the approved quote-sized Spot Market Buy. This
does not make the repository authorized or ready: the checked-in execution
safety configuration deliberately disables live execution, keeps the kill
switch active, and declares order submission not implemented.

## Architecture and boundaries

V1 remains the immutable deterministic BTC Adaptive DCA strategy: `$10`
core/minimum, drawdown bands of `$10/$25/$50/$75/$100`, configured Fear &
Greed multipliers, `ROUND_HALF_UP`, a `$500` calendar-month cap, no
carry-forward, and Bybit Spot BTCUSDT identity. Strategy changes require a
separately versioned proposal; none are made here.

The canonical execution ledger is the sole source of confirmed spend and
monthly totals. Decisions, recommendations, intents, manifests, approvals,
attempts, acknowledgements, notifications, and partial fills are not ledger
rows. Only authoritative confirmed fills can produce a final execution that
is appended exactly once; historical ledger records are not edited.

Canonical V1 market data uses Bybit Spot BTCUSDT only. There is no
cross-exchange fallback. Missing, stale, or contradictory inputs fail closed.

The execution chain preserves distinct artifacts:

```text
Decision
!= OrderIntent
!= CanaryManifest
!= LiveApproval
!= SubmissionAttempt
!= SubmissionOutcome
!= SubmissionReconciliation
!= Execution
!= Ledger
```

The canary is a narrowly scoped manual preparation path. Its immutable
manifest binds one V1 decision and exact order identity. A persisted,
short-lived, one-shot approval is separate from the manifest and from
authorization. The live engine revalidates current state, persists an
immutable attempt before a possible network boundary, sends at most one
narrowly constructed Spot request, never retries ambiguous transport, and
requires fresh exact-identity reconciliation. An ACK is not a fill.

## Quote-sized order contract

For the exact payload `category=spot`, `symbol=BTCUSDT`, `side=Buy`,
`orderType=Market`, `marketUnit=quoteCoin`, and `isLeverage=0`, `qty` is the
approved USDT amount. No base-to-quote conversion is introduced.

`maxOrderAmt` is deprecated and unused. `maxMarketOrderQty` remains quantity
metadata and is never multiplied by ticker price or treated as a quote-USDT
maximum. No wallet-derived or ticker-derived maximum is fabricated.

The lack of a separate quote maximum can cause an explicit exchange rejection,
but cannot create an overspend path. An explicit rejection is recorded as
`exchange_rejected`/`rejected_by_exchange`, is never retried or topped up,
assumes no fill, and never enters the ledger. Bybit acceptance is not
guaranteed.

## Evidence, blocker, and operations

Production evidence is immutable, digest-protected, expiring, build- and
host-bound, and stores sanitized read-only evidence. Evidence cannot grant
authorization. The truthful quote capability state is
`quote_unit_maximum_supported=false` and
`quote_unit_maximum_required=false`; the production quote-limit conclusion is
`QUOTE_UNIT_MAX_NOT_REQUIRED`. The provider may report `NOT_EXPOSED`, but that
is informational capability state, not an active blocker.

The account-scoped non-borrowed Spot availability check
`/v5/order/spot-borrow-check:spotMaxTradeAmount` remains mandatory. So do the
monthly cap, instrument minimum, exact identity and approval, no liabilities,
fresh evidence, duplicate/ambiguous-submission protection, reconciliation,
and exactly-once ledger rules.

Operational health must not describe the obsolete quote blocker as an external
dependency. Other independent configuration, safety, freshness, liability,
availability, reconciliation, or evidence failures may still make readiness
or preauthorization `NOT_READY`/`BLOCKED`.

Production status snapshots are immutable, digest-protected operational
checkpoints. Deterministic alerts derive from adjacent persisted snapshots;
Telegram is downstream notification only and is never status, authorization,
or spend truth.

## What is complete

- V1 rules, canonical ledger, execution identity, and disabled production
  defaults are preserved.
- Quote-sized order interpretation now matches the reviewed Bybit Spot API
  contract without inventing a quote maximum.
- Readiness, evidence, blocker reporting, no-POST recovery, operational audit,
  status history, and crash-recoverable status alerts remain implemented.
- Closeout documentation reflects the non-required quote maximum policy
  without adding execution capability or authorization.

## What is intentionally blocked

Real-money execution remains blocked by the checked-in safety configuration:
`live_execution_enabled=false`, `kill_switch=true`, and
`order_submission=not_implemented`. Separate real-money authorization also
remains `NOT_AUTHORIZED`.

The obsolete `BYBIT_QUOTE_UNIT_MAX_NOT_EXPOSED` dependency is not an active
blocker. Readiness must still fail or remain unavailable when any other
required safety check is missing, stale, contradictory, unsafe, or otherwise
unproven. Removing this one blocker does not bypass those checks.

## Before any future real-money activation

Follow the ordered prerequisites in
[`PRODUCTION_OPERATIONS_RUNBOOK.md`](PRODUCTION_OPERATIONS_RUNBOOK.md): verify
the checked-in safety configuration and exact operation; collect fresh
account-scoped availability, liability, identity, instrument, approval, and
reconciliation evidence; confirm the monthly cap and minimum amount; confirm
no unresolved or ambiguous prior submission; and obtain separate explicit user
authorization for the exact order. No evidence bundle, approval artifact,
environment variable, config change, or merge can substitute for that
authorization. Do not activate unattended execution or scheduling.

## Audit baseline

The canonical ledger baseline inspected for closeout contains 12
active/confirmed canonical executions: September 2026 `$220`, October 2026
`$95`, cumulative `$315`. No historical row was changed. The checked-in
safety configuration remains `false / true / not_implemented` for live
enablement, kill switch, and order submission respectively.

The closeout audit is repository-local and read-only. It does not make
production Bybit requests, submit an order, create a live approval, or mutate
the ledger.
