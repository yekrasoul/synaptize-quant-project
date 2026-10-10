# Phase 5.4B operator runbook

This is a future-use procedure. The current repository is intentionally
disabled and unauthorized.

1. Collect read-only readiness and canonical artifacts.
2. Prepare the controlled canary and inspect the exact V1 amount.
3. Run `canary-approve` to persist one short-lived `LiveExecutionApproval`.
4. Human review verifies the artifact ID, SHA, amount, symbol, and client order
   ID.
5. A future explicit authorization ceremony binds a one-shot authorization to
   that exact approval; it cannot be standing authorization.
6. Re-read all sources at Gate A.
7. Re-read all sources again at Gate B immediately before any future network
   mutation.
8. Persist the immutable submission-attempt artifact first.
9. Submit at most once using the exact Spot quote-coin contract.
10. Reconcile the same client order ID. Unknown, partial, or contradictory
    evidence stops the process.
11. Only canonical confirmed execution evidence may be handed to a separate
    ledger-append step.

Rollback/recovery is always fail-closed: enable the kill switch, stop on stale
or mismatched evidence, preserve attempt artifacts, reconcile ambiguous
operations before any further action, and never resize, split, retry, or amend
the order.
