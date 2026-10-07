# Phase 5.5 — Controlled Operations & Recovery

> MERGING PHASE 5.5 DOES NOT AUTHORIZE A REAL BTC ORDER.

Phase 5.5 adds manual operator visibility and recovery around the reviewed
Phase 5.4B engine. It does not schedule, retry, resize, top up, or autonomously
submit an order.

## Commands

- `ops-status` reports conservative operational state and fresh ledger budget.
- `ops-plan` reports the only safe next action and prohibited actions.
- `ops-health` validates configuration, ledger, and immutable artifact storage.
- `audit-run RUN_ID` reconstructs the immutable evidence chain.
- `canary-approve` creates an exact, five-minute manifest-bound approval after
  requiring canary ID, manifest digest, amount, client order ID, and payload digest.
- `canary-execute` requires exact run/canary/approval/digest inputs and delegates
  solely to the Phase 5.4B engine. Checked-in production defaults block it.
- `reconcile-existing` has no submission transport and performs recovery only.

All read-only commands support `--json`. Exit codes are: `0` safe success,
`2` blocked invariant, `3` reconciliation required, `4` unavailable/ambiguous,
and `5` integrity corruption.

## State model

`IDLE → PREPARED → APPROVED → ATTEMPT_RECORDED → CONFIRMED` is permitted only
through immutable artifacts and authoritative reconciliation. `SUBMISSION_UNCERTAIN`,
`ACTIVE`, `PARTIAL`, and any unknown state lead to `RECONCILIATION_REQUIRED`.
`BLOCKED` is terminal until a new safe preparation is available. An ACK is never
a fill.

## Exposure and recovery

Status distinguishes canonical confirmed ledger spend from known partial-fill
quote value and potential effective remaining budget. This is operational risk
information only and does not alter V1's $500 calendar-month accounting.
Recovery reads the exact persisted manifest and approval, validates digests,
uses fresh Bybit order/fill reads, preserves reconciliation snapshots, and
appends a final aggregate execution only after a fully confirmed fill set.

## Safety boundary

Checked-in settings remain `live_execution_enabled: false`, `kill_switch: true`,
and `order_submission: not_implemented`. There is no scheduled live workflow.
Real-money execution still requires separate explicit user authorization.
