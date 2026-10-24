# Test Suite Audit

V2 adds contract tests for: venue-neutral mapping boundary, $500 cap and kill
switch, account identity, unresolved execution, exactly-once claim, ambiguous
timeout prohibition, reconciliation-before-accounting, runtime policy drift,
and dependency direction.

Legacy tests remain while legacy production code remains reachable. No test was
deleted merely to increase the apparent simplification rate. Tests that freeze
old workflow or guardian structure were already removed in PR #76.

Durable V2 tests cover crash after claim, timeout/reset/outage ambiguity,
deterministic rejection, recovery without a venue order ID, fresh kill-switch
recheck, duplicate identity across restart, and idempotent accounting posting.
It additionally uses two spawned processes against one repository root to
exercise the filesystem-exclusive claim rather than a shared in-memory lock.
Service-level contention verifies that a `SubmissionAlreadyClaimed` outcome
reloads the durable winner state without a lifecycle write or network call.

The parity suite adds fixture-backed checks for legacy Bybit observable request
semantics versus V2 CCXT mapping, native minimum/precision checks,
acknowledgement/client-identity reconciliation, fills/fees, deterministic
rejection, uncertain network errors, canonical ExecutionRecord schema
round-trip, and Production Shadow's no-claim/no-POST V2 preflight.
