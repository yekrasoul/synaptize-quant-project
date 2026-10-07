# Phase 5.4B — Controlled Canary Live Execution

This phase adds the narrowly scoped execution seam for one exact Phase 5.4A
manifest. It remains fail-closed by the checked-in configuration:

```yaml
live_execution_enabled: false
kill_switch: true
order_submission: not_implemented
```

`LiveApproval` is a separate immutable artifact read back from `ArtifactStore`.
The manifest is likewise read back and digest-verified before execution. It binds the canary ID,
manifest SHA-256, V1 Decision and OrderIntent identities, deterministic
`orderLinkId`, exact quote amount, payload fingerprint, Spot BTCUSDT identity,
and a five-minute maximum expiry. It is not standing authorization and is
never created by the normal shadow or preparation paths.

The future manually invoked path revalidates the manifest, ledger budget,
execution history, credential/account/wallet state, exact authoritative Spot
quote-buy availability, instrument rules, and fresh `orderLinkId` absence. It
then persists the full-identity `OrderSubmissionAttempt` before the single allowed POST. Every
transport ambiguity blocks retry and requires reconciliation.

An ACK creates only a `SubmissionOutcome`. A separate fresh order-and-fill
reconciliation must return authoritative fill records before an `Execution`
can be appended to the canonical ledger. Actual BTC quantity, quote value,
price, fee, and exchange identifiers are retained. Ledger append uses a
process lock, durable fsync, and a recoverable execution-identity claim.
Partial, active, missing,
contradictory, or ambiguous evidence never writes the ledger.

No scheduled live execution, production workflow, V1 strategy change, manual
ledger rewrite, or real Bybit POST is part of this phase.
