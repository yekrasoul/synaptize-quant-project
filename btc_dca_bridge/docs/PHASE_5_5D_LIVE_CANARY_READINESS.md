# Phase 5.5D — Live Canary Readiness

`bybit-canary-readiness` is a read-only audit for the future Bybit Spot
BTCUSDT canary. It reads `BYBIT_API_KEY` and `BYBIT_API_SECRET` from the
runtime environment only and never prints them or persists them.

The audit uses authenticated GET-only evidence for credential scope, account
mode, wallet/liability state, authoritative Spot USDT availability, BTCUSDT
instrument metadata, server time, and harmless empty order/execution probes.
It also validates the canonical ledger, unresolved submission evidence,
Production Shadow health, and the checked-in fail-closed execution policy.

Results are deterministic:

- `CANARY_READY_READ_ONLY` means the read-only evidence set passed. It does
  not authorize, activate, enable, or submit an order.
- `CANARY_NOT_READY` includes explicit blockers and is the expected current
  state when credentials/evidence or the required lifecycle state is absent.

The command never calls `POST /v5/order/create`, never writes the ledger, and
does not create approval, activation, enablement, or submission artifacts.
The checked-in state remains `live_execution_enabled: false`,
`kill_switch: true`, `order_submission: not_implemented`, with Authorization
`NOT_AUTHORIZED`, Activation `NOT_ACTIVATED`, Production Enablement
`NOT_ENABLED`, and Execution `BLOCKED`.
