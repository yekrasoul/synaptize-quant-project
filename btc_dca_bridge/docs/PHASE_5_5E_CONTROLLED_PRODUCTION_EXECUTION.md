# Phase 5.5E — Controlled Production Execution Wiring

`canary-execute` is now the composition point for the existing guarded
execution pipeline. It does not create ceremony artifacts and it does not
change the checked-in execution policy.

The exact future sequence is:

1. Load and verify the immutable CanaryManifest, LiveExecutionApproval,
   ProductionEnablement, RealMoneyAuthorization, ExecutionActivation,
   Decision, OrderIntent, and production-evidence bytes and SHA sidecars.
2. Evaluate Gate D against the production policy and current runtime safety.
3. Re-read fresh account, credential, liability, availability, instrument,
   ledger, and reconciliation evidence for Gate A.
4. Re-read those dynamic sources for Gate B and reject any fingerprint drift.
5. Persist the immutable SubmissionAttempt.
6. Evaluate Gate C immediately before the network boundary.
7. Construct the authenticated Bybit transport lazily and allow at most one
   exact Spot BTCUSDT Market Buy POST.
8. Reconcile the exact orderLinkId/orderId and aggregate authoritative fills.
9. Append the canonical ledger only after confirmed FILLED reconciliation.

Current checked-in defaults stop at Gate D:

```text
live_execution_enabled: false
kill_switch: true
order_submission: not_implemented
Production Enablement: NOT_ENABLED
Authorization: NOT_AUTHORIZED
Activation: NOT_ACTIVATED
Execution: BLOCKED
NO ORDER EXECUTED
```

Expired, mismatched, stale, partial, ambiguous, or unresolved evidence
returns a blocked or reconciliation-required result. No retry may construct a
second order for the same immutable identity. Transport construction is
deferred until Gate C succeeds, and the existing reconciliation/ledger
confirmation boundary remains authoritative.
