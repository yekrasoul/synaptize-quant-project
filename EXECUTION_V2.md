# Execution V2

Canonical states are `CREATED -> RISK_APPROVED -> SUBMISSION_STARTED ->
SUBMITTED -> RECONCILING -> CONFIRMED`. Terminal alternatives are `BLOCKED`,
`FAILED_PRE_SUBMISSION`, `REJECTED`, `AMBIGUOUS`, `PARTIALLY_FILLED`, and
`CANCELLED`.

The OMS claims a client-order identity before the network boundary. A timeout
after that boundary becomes `AMBIGUOUS`; it cannot transition to a new
submission. Only reconciliation may subsequently establish venue truth.

`btc-dca execute --decision-json ...` is the V2 non-interactive entry point.
The committed policy blocks it before credentials or network access. Enabling
live submission is intentionally deferred until CCXT/Bybit parity fixtures and
durable V2 execution-record persistence are accepted.
