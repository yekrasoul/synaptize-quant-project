# Phase 6.1 — Final Production Closeout & Handoff

## Authoritative state

```text
software_health = HEALTHY_BLOCKED_EXTERNAL_DEPENDENCY
production_readiness = NOT_READY
preauthorization = BLOCKED
real_money_authorization = NOT_AUTHORIZED
external_blocker = BYBIT_QUOTE_UNIT_MAX_NOT_EXPOSED
live_execution_enabled = false
kill_switch = true
order_submission = not_implemented
```

This is the intended successful closeout state: the software is internally
healthy and safely blocked on an external contract limitation. It is not an
unfinished software defect and it does not authorize a real order.

## Architecture and boundaries

V1 is the immutable deterministic BTC Adaptive DCA strategy: $10 core/minimum,
drawdown bands of $10/$25/$50/$75/$100, the configured Fear & Greed multipliers,
`ROUND_HALF_UP`, a $500 calendar-month cap, no carry-forward, and Bybit Spot
BTCUSDT identity. Strategy changes require a separately versioned proposal;
none are made here.

The canonical execution ledger is the sole source of confirmed spend and
monthly totals. Decisions, recommendations, intents, manifests, approvals,
attempts, acknowledgements, notifications, and partial fills are not ledger
rows. Only authoritative confirmed fills can produce a final execution that
is appended exactly once; historical ledger records are not edited.

Canonical V1 market data uses Bybit Spot BTCUSDT only. There is no
cross-exchange fallback: if Bybit is unavailable, unreliable, stale, or
contradictory, V1 fails closed and produces no decision. Within a single run,
current price and rolling 7-day high must come from the same Bybit spot-market
source. Perpetuals, futures, mark/index prices, derivatives, leveraged
products, other exchanges, and generic blended BTC prices remain prohibited
substitutes. The configured sentiment source is Crypto Fear & Greed Index.
Missing, stale, or contradictory inputs fail closed rather than silently
changing market type or mixing sources.

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

## Evidence, blocker, and operations

Production evidence is immutable, digest-protected, expiring, build- and
host-bound, and stores sanitized read-only evidence. Evidence cannot grant
authorization. The production quote-limit source allowlist is empty. The
current Bybit provider truthfully reports `NOT_EXPOSED`; `market_buy_quote_maximum`
remains `None`. Base quantity multiplied by a price, wallet arithmetic, and
the state-changing pre-check endpoint are not accepted substitutes.

`BYBIT_QUOTE_UNIT_MAX_NOT_EXPOSED` is therefore classified as an external
market-contract blocker. Blocked-production mode keeps software health
distinct from production readiness: readiness remains `NOT_READY`,
preauthorization remains `BLOCKED`, canary preparation is blocked, and live
submission cannot pass the quote-limit gate.

Reconciliation is the only allowed next action for unresolved prior
submission identities. Active and partial order evidence is preserved
immutably, does not trigger retry/top-up, and does not count as completed
ledger spend. Final completion requires authoritative fills and exactly-once
ledger handling.

Production status snapshots are immutable, digest-protected operational
checkpoints. Deterministic alerts derive from adjacent persisted snapshots;
missing alerts can be reconstructed after a crash. Telegram is downstream
notification only and is never status, authorization, or spend truth.

## What is complete

- Phases 1–4, Phase 5.1–5.9, and Phase 6.0 are complete and retained.
- V1 rules, canonical ledger, execution identity, and disabled production
  defaults are preserved.
- Readiness, evidence, blocker reporting, no-POST recovery, operational audit,
  status history, and crash-recoverable status alerts are implemented.
- Closeout adds the operator index and handoff documentation without adding
  execution capability.

## What is intentionally blocked

An authoritative quote-denominated upper bound for the exact Bybit BTCUSDT
Spot Market Buy with `marketUnit=quoteCoin` is not exposed by an approved
source. Accordingly production readiness is `NOT_READY`, preauthorization is
`BLOCKED`, and no real-money authorization exists. This is an external
dependency boundary, not a reason to weaken validation or estimate a limit.

## Before any future real-money activation

Follow the ordered prerequisites in
[`PRODUCTION_OPERATIONS_RUNBOOK.md`](PRODUCTION_OPERATIONS_RUNBOOK.md):
official-source research and manual review; explicit reviewed source-policy
and validator changes; passing tests and merged review; fresh production
evidence; readiness and preauthorization passing; then separate explicit
user authorization for the exact order. No evidence bundle, approval artifact,
environment variable, config change, or merge can substitute for that
authorization. Do not activate unattended execution or scheduling.

## Audit baseline

The canonical ledger baseline inspected for closeout contains 12
active/confirmed canonical executions: September 2026 `$220`, October 2026
`$95`, cumulative `$315`.
No historical row was changed. The checked-in safety configuration remains
`false / true / not_implemented` for live enablement, kill switch, and order
submission respectively.

The closeout audit is repository-local and read-only. It does not make
production Bybit requests, submit an order, create a live approval, or mutate
the ledger. Fresh account-specific production evidence must be collected by an
operator only through the established read-only readiness/evidence commands
when separately required.
