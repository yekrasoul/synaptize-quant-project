# Phase 5.1 — Safe Execution Foundation

The execution domain is deliberately separate from the V1 `Decision` and the
canonical ledger `Execution`. The lifecycle is `Decision -> OrderIntent ->
SafetyValidation -> (future) OrderSubmission -> fills -> reconciliation ->
confirmed ledger execution`.

Current state: **LIVE ORDER SUBMISSION NOT IMPLEMENTED**. `execution-plan`
constructs a deterministic intent, rereads the canonical ledger, performs the
double cap check, and emits planning evidence. It always prints `NO ORDER
EXECUTED` and cannot call an order-create endpoint.

The client order ID is a stable SHA-256-derived Bybit-safe identifier from
strategy, decision, and logical run identity. A timeout is treated as
ambiguous: record ambiguity, look up by client ID, reconcile if found, and do
not retry until absence is conclusive. Reconciliation aggregates zero,
partial, multiple, or full confirmed fills; only confirmed fills may become
ledger entries.

`config/execution.yaml` ships with `live_execution_enabled: false` and
`kill_switch: true`. Future credentials must be runtime/GitHub Secrets only,
Spot trading only, with withdrawals, transfers, derivatives, margin, and
leverage disabled; IP restrictions are recommended.
