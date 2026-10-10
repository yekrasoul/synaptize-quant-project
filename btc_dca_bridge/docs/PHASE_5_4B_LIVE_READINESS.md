# Phase 5.4B live-readiness boundary

Phase 5.4B prepares evidence for one possible future Bybit Spot BTCUSDT
market-buy. It does not authorize or submit an order. The checked-in state
remains `live_execution_enabled: false`, `kill_switch: true`,
`order_submission: not_implemented`, and `Authorization: NOT_AUTHORIZED`.

## Lifecycle

1. The read-only canary, Decision, and OrderIntent are bound by IDs and
   SHA-256 fingerprints.
2. `LiveExecutionApproval` is persisted append-only with a maximum ten-minute
   lifetime. It is an approval-preparation artifact, not authorization. Its
   payload contains no credentials or signatures and its ArtifactStore receipt
   supplies the immutable SHA-256 sidecar.
3. A future, separately verifiable human authorization ceremony must name the
   approval ID and SHA, exact amount, and deterministic client order ID. No
   branch, environment variable, Telegram message, dispatch, or passing test
   can substitute for that ceremony.
4. `LiveExecutionGate` evaluates fresh evidence at Gate A and again at Gate B.
   A changed commit, month, ledger, budget, account, credential scope,
   liability, availability, instrument, clock, contract, or reconciliation
   fingerprint blocks the second gate.
5. Only after both gates pass may a future implementation persist a
   `PREPARED_FOR_SUBMISSION` attempt artifact. That artifact is the last
   durable action before a future network mutation.
6. Timeout, reset, malformed response, partial fill, or any unknown exchange
   result is `RECONCILIATION_REQUIRED`; it cannot be retried automatically.
   Reconciliation must prove the exact client order identity and terminal
   confirmed fills before a separate ledger append step can be considered.

The Phase 5.4B module has no HTTP transport and does not write the ledger.
`btc-dca live-readiness` is a read-only command that reports the current
blocked boundary.

## Future operator runbook

Collect readiness evidence, prepare the canary, prepare and review the exact
approval, invoke the separately documented authorization ceremony, run Gate A,
run Gate B immediately before the one-shot submit, reconcile the exact client
order ID, and append to the canonical ledger only after confirmed reconciliation.

## Recovery and rollback

Activate the kill switch for any concern. For an ambiguous submission, preserve
the attempt artifact and perform read-only reconciliation; never send a second
request. Partially filled or contradictory evidence remains reconciliation
required. Account mismatch, stale evidence, month rollover, cap exhaustion, or
artifact corruption requires a new preparation cycle. No recovery path bypasses
the gates or edits historical ledger rows.
