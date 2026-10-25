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

## Durable recovery and network boundary

V2 persists an execution identity, then persists `SUBMISSION_STARTED`, before
an EMS call may write to a venue. `SUBMISSION_STARTED`, `SUBMITTED`,
`RECONCILING`, `AMBIGUOUS`, and `PARTIALLY_FILLED` are recovery-only states.
On restart they are reconciled by canonical client identity; they are never
submitted again. A failure before the durable submission claim may become
`FAILED_PRE_SUBMISSION`. Any error after a possible network write is
`AMBIGUOUS`, except an explicit `DeterministicVenueRejection`.

## Exactly-once durability

`JsonExecutionRepository` creates a per-client-identity submission claim with
`O_CREAT | O_EXCL`, fsyncs the claim file and its directory, then atomically
replaces the execution record with `SUBMISSION_STARTED` and fsyncs the
directory again. Independent processes therefore have one winner; any existing
claim blocks a second network write forever. If a crash occurs between claim
creation and record replacement, repository reads project the claimed
`RISK_APPROVED` record as `SUBMISSION_STARTED`, forcing recovery-only
reconciliation.

At the application boundary, `SubmissionAlreadyClaimed` is distinct from a
pre-claim validation failure. The losing service loads and returns the
canonical durable order unchanged with `DUPLICATE_OR_RECONCILIATION_REQUIRED`;
it never turns its stale `RISK_APPROVED` value into `FAILED_PRE_SUBMISSION`.

## Canonical execution evidence

The repository projects each lifecycle state into the `execution_record_v2`
schema when audit evidence is required. The projection contains only canonical
identity, strategy/decision linkage, venue/account/environment identity,
lifecycle, claim, acknowledgement/reconciliation references, and ambiguity
or rejection reason, normalized fills/fees, and an observed submission time
when supplied. It intentionally excludes keys, secrets, signatures, and raw headers.
It does not replace the durable repository as the mutable state owner.

## Shared pre-submit boundary

`plan_pre_submit` is the common path for production and Shadow:
`OrderIntent -> current RiskContext -> RiskEngine -> VenueAdapter.validate_intent
-> VenueAdapter.prepare_submission`. Production creates its durable claim only
after an approved plan. Shadow stops at the plan, never creates a claim, and
never calls `submit_once`. If Shadow cannot safely obtain current authenticated
account or venue context, it reports `NOT_VERIFIED`; it does not invent a
blocked account state.

This shared-path capability is VERIFIED by tests. Operational Production Shadow
parity is only PARTIALLY VERIFIED until the runtime can supply safe current
account and venue context; its current `NOT_VERIFIED` output is intentional.

## CCXT signed pre-transport boundary

The V2 writer is characterized without credentials in
`test_ccxt_wire_boundary.py`. One test runs the actual pinned CCXT 4.5.90
Bybit `create_order` implementation with dummy credentials and replaces only
the exchange instance's final `fetch(url, method, headers, body)` transport.
The test captures CCXT's signed request after `sign()` and before any network
I/O, proving the V5 method/path, JSON body, authentication-header structure,
timestamp/receive-window fields, and canonical Spot quote-buy parameters.
The socket-connect guard proves zero network writes. Separate tests continue to
prove deterministic rejection versus ambiguous timeout semantics and no
automatic re-submit.

This verifies the pinned CCXT offline signed representation, not an
authenticated production account/environment, withdrawal exclusion, live
response parity, or operational Production Shadow parity. V2 remains
fail-closed and has no production CLI composition on committed main.
