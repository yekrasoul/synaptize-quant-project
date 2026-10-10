# Phase 5.4C explicit authorization

Phase 5.4C adds the human authorization ceremony for one exact prepared
execution identity. It does not enable execution and does not submit an order.
The runtime remains `NOT_AUTHORIZED` because the checked-in execution config is
still disabled and the kill switch is still active.

The terms are intentionally separate:

- `CanaryManifest`: prepared candidate and read-only evidence.
- `LiveExecutionApproval`: reviewed/prepared execution envelope; not
  authorization.
- `RealMoneyAuthorization`: explicit, short-lived, one-shot operator action.
- `SubmissionAttempt`: immutable pre-network evidence.
- `SubmissionOutcome`/`Reconciliation`: execution evidence.

`canary-approve` now persists the canonical `LiveExecutionApproval` required by
this phase. The older `LiveApproval` remains readable only for legacy recovery
artifacts and cannot satisfy the Phase 5.4C gate by itself.

## Ceremony

`live-authorize` requires the persisted Phase 5.4B approval ID and SHA, the
CanaryManifest identity, exact amount, exact deterministic client order ID,
UTC month, a unique run ID, and a confirmation matching:

`AUTHORIZE BYBIT SPOT BTCUSDT BUY $<exact-amount> <client-order-id>`

Generic confirmations such as `yes`, `approve`, `go`, `confirm`, and `بزن بریم`
are rejected. Only a SHA-256 digest of the matching confirmation is persisted;
raw operator text is discarded.

The resulting artifact is append-only, has a maximum five-minute lifetime,
contains no credentials or secrets, and is bound to the exact Phase 5.4B
approval, repository commit, safety configuration, contract, account, month,
and one-shot identity. A second authorization, prior attempt, ambiguous
outcome, unresolved reconciliation, or confirmed execution blocks replay.

The artifact status `AUTHORIZED` means only that the ceremony was completed.
It does not mean the system is globally authorized to trade. Gate A and Gate B
must independently verify the persisted authorization and all live safety
evidence, while current `live_execution_enabled: false`, `kill_switch: true`,
and `order_submission: not_implemented` continue to block execution.

No workflow, test, Telegram interaction, branch name, environment variable,
CanaryManifest, passing CI, or informal operator phrase invokes this ceremony.
