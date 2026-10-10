# Phase 5.5C — Bybit Spot submission persistence and reconciliation

Phase 5.5C adds durable evidence around the existing guarded Spot submission
boundary. It does not enable the checked-in execution configuration and it has
no ledger-writing path.

## Evidence lifecycle

1. Before a POST could occur, an immutable `ORDER_SUBMISSION_ATTEMPT` is
   written with the exact `orderLinkId`, request fingerprint, quote amount,
   and approved identity bindings.
2. The POST acknowledgement is written as an immutable
   `ORDER_SUBMISSION_OUTCOME`. An acknowledgement means acceptance only; it
   is never a fill.
3. Fresh authenticated reads of `/v5/order/realtime` and
   `/v5/order/history`, followed by `/v5/execution/list`, produce an immutable
   `SUBMISSION_RECONCILIATION` snapshot.

The attempt journal is append-only and digest-backed through `ArtifactStore`.
On restart, any prior attempt for the same deterministic client order identity
blocks another submission. A timeout, missing order, partial fill, conflicting
identity, or malformed evidence remains unresolved and requires later
reconciliation; it is never retried automatically.

## Reconciliation states

The normalized states are `SUBMISSION_UNKNOWN`, `ACCEPTED`, `NEW`,
`PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`, and `UNRESOLVED`.
Only exact Spot `BTCUSDT` order identity with matching execution IDs,
quantities, values, and a final `FILLED` state is eligible for the existing
confirmation-only ledger path. This module itself never appends a ledger
execution.

The production defaults remain `live_execution_enabled: false`,
`kill_switch: true`, and `order_submission: not_implemented`; therefore no
real POST is reachable in the checked-in repository.
