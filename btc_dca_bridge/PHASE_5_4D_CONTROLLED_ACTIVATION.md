# Phase 5.4D — Controlled Activation

Phase 5.4D adds the final short-lived, one-shot capability boundary around
the existing guarded `LiveOrderEngine`. It does not enable the checked-in
execution configuration and it does not submit an order.

## Separate lifecycle

1. `CanaryManifest` prepares one candidate.
2. `LiveExecutionApproval` binds the preparation evidence.
3. `RealMoneyAuthorization` records explicit human authorization.
4. `ExecutionActivation` records a separate exact, 60-second activation.
5. Gate A revalidates the immutable and current evidence.
6. Gate B revalidates immediately before the submission-attempt boundary.
7. `SubmissionAttempt` is persisted before any possible network mutation.
8. Gate C rechecks the activation, capability, request identity, expiry, and kill switch.
9. The existing transport can be called at most once by a future enabled runtime.
10. Existing reconciliation determines whether execution is confirmed.
11. The ledger may be appended only after canonical confirmed reconciliation.

Authorization is not activation. Activation is not execution. Execution is not
a confirmed fill. A confirmed fill is not a ledger mutation until the existing
reconciliation policy permits it.

## Activation

`live-activate` requires the exact persisted authorization and approval IDs,
their SHA-256 values, the exact amount, month, client order ID, and a phrase of
the form:

```text
ACTIVATE ONE SHOT BYBIT SPOT BTCUSDT BUY $25 <client-order-id>
```

Only a digest of that phrase is persisted. The activation expires within 60
seconds. An expired unused activation is never renewed; a new activation can
be prepared only while the original authorization remains valid and no
attempt, outcome, or unresolved reconciliation evidence exists.

`live-activation-status` is read-only. It derives `ACTIVE`, `EXPIRED`,
`CONSUMED`, `REVOKED`, or `INVALID` from immutable artifacts and evidence.

## Failure and recovery

| Condition | Result | Recovery |
| --- | --- | --- |
| Kill switch active | Block before capability or transport | Keep the switch active and investigate |
| Authorization or activation expired | Block | Re-run preparation and human review |
| Evidence or account identity changed | Block | Recollect and revalidate all bound evidence |
| Attempt already exists | Consumed / reconciliation required | Reconcile the existing identity; never retry automatically |
| Timeout, reset, malformed response | Reconciliation required | Query the same client order ID before any further action |
| Partial or unknown exchange state | Reconciliation required | Complete authoritative reconciliation |
| Monthly cap or ledger invalid | Block | Correct through the governed ledger/recovery process |
| Artifact corruption | Fail closed | Preserve artifacts and perform operator recovery |

The checked-in defaults remain `live_execution_enabled: false`,
`kill_switch: true`, `order_submission: not_implemented`, and
`Authorization: NOT_AUTHORIZED`.
