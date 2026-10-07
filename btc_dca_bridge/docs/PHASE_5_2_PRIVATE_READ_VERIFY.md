# Phase 5.2 — Private Bybit Read/Verify Layer

Phase 5.2 provides authenticated, read-only Bybit V5 verification. It does
not submit, amend, cancel, transfer, withdraw, borrow, repay, or change any
account setting. Every private request is a signed `GET` from an explicit
allowlist. The `private-verify` CLI prints `READ ONLY — NO ORDER EXECUTED`.

## Scope and endpoints

The allowlist is:

- `GET /v5/user/query-api`
- `GET /v5/account/info`
- `GET /v5/account/wallet-balance`
- `GET /v5/order/realtime`
- `GET /v5/order/history`
- `GET /v5/execution/list`
- `GET /v5/market/instruments-info` (public market metadata)

There is no arbitrary private-path method and no non-GET transport method.

## Credentials and permissions

The client reads `BYBIT_API_KEY` and `BYBIT_API_SECRET` from the runtime
environment. Secrets are never printed, persisted, or included in artifacts
or exception messages. Credential information is classified as
`READ_ONLY`, `TRADE_CAPABLE`, `UNSAFE_PERMISSION_SCOPE`, or `INVALID`.
Withdrawal, transfer, borrow, and repay permissions are unsafe. Permissions
are never changed automatically; use a dedicated key with no withdrawal or
transfer scope.

## Verification and ambiguity

Verification reads account mode, wallet balances, BTC/USDT liabilities, and
authoritative Bybit Spot BTCUSDT metadata. Balance values are parsed as
`Decimal`; borrow and accrued-interest values are surfaced and never alter V1
allocation rules.

Order and execution reads use the exact deterministic `orderLinkId`. A
confirmed fill is `confirmed`; active, partial, contradictory, or uncertain
evidence is `ambiguous`; only successful authoritative reads showing no order
and no fills can be `conclusively_absent`. Authentication, timeout, malformed
response, and API failures never become absence.

Phase 5.2 does not write the canonical ledger or create fake execution,
submission, or fill artifacts. It remains behind the Phase 5.1 gates:
`live_execution_enabled=false`, `kill_switch=true`, and
`order_submission=not_implemented`.
