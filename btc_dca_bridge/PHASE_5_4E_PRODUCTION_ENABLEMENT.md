# Phase 5.4E — Production Enablement

`ProductionEnablement` is a short-lived, append-only governance artifact. It
does not authorize an order, activate an order, or change `execution.yaml`.
The checked-in policy remains `live_execution_enabled: false`,
`kill_switch: true`, and `order_submission: not_implemented`.

The theoretical controlled path is:

`Production Enablement → Gate D → RealMoneyAuthorization → ExecutionActivation → Gate A → Gate B → SubmissionAttempt → Gate C → one POST → reconciliation → ledger confirmation`

Each boundary is identity-bound. Gate D requires current read-only evidence,
the exact Bybit Spot BTCUSDT Buy/Market/quoteCoin contract, a $500 cap, no
leverage, no standing permission, and no retry. The kill switch is absolute at
every boundary. The attempt is durable before the transport factory may be
invoked; an ambiguous result requires reconciliation and cannot be retried.

Enablement is not authorization; authorization is not activation; activation
is not execution; an exchange acknowledgement is not a confirmed fill; and a
confirmed fill is not a ledger append until reconciliation policy confirms it.

`production-enable-prepare` is a governance-only ceremony requiring the exact
phrase `ENABLE CONTROLLED BYBIT SPOT BTCUSDT ONE-SHOT EXECUTION POLICY`. It
stores only a digest. `production-enablement-status` is read-only.

Rollback is fail-closed: turn the kill switch on, stop new work, preserve all
immutable evidence, reconcile unresolved identities, and never delete or
retry a submission attempt.
