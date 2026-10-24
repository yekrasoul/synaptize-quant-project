# Execution Platform V2 Migration Report

## Delivered in this phase

- Added CCXT as the contained CEX dependency and `VenueAdapter` boundary.
- Added canonical domain models, event envelope, OMS, execution states, risk,
  accounting gate, Adaptive DCA strategy seam, and a fail-closed runtime policy.
- Added `btc-dca execute` as a non-interactive V2 command. It safely blocks in
  the committed configuration before any credential or network access.

## Verification

Baseline was 585 unit tests. The durable V2 work raises the full suite to 602;
the complete suite and canonical validation pass locally. Graphify update was attempted as
required by repository guidance, but the `graphify` executable is unavailable
in this checkout environment, so no graph output was claimed or changed.

## Not yet retired

The three legacy Bybit transports, legacy reconcilers, artifact ceremony, and
readiness projections are not removed in this commit. Removing them before
durable V2 submission/evidence parity would risk the exact safety controls this
migration is meant to preserve. They are explicitly legacy debt, not V2 owners.

## Remaining gates

Before activation: obtain a controlled, non-production Bybit/CCXT signed-wire
and response capture; verify credential scope/account identity; then route all
production submission through V2 and remove the legacy submit path in a
separately reviewable change.

## Durable safety layer

This phase adds a V2-only durable execution repository, crash/restart recovery,
client-identity reconciliation, fresh-risk provider, venue-health risk input,
and idempotent accounting repository. This branch adds a schema-validated
`execution_record_v2` projection and read-only Production Shadow V2
planning/mapping parity. Live request-byte parity and cutover from the legacy
submit path remain unverified activation blockers; V2 remains fail-closed.
The full matrix is in [`BYBIT_CCXT_CUTOVER.md`](BYBIT_CCXT_CUTOVER.md).

The durable claim is filesystem compare-and-set, not an in-memory lock:
exclusive creation of a per-client identity claim file (`O_CREAT|O_EXCL`) plus
file and directory fsync. A spawned two-process test proves exactly one
contender reaches the simulated network boundary and restart cannot claim it
again.

The service-level race is also covered with two independent processes. One
receives submission ownership and reaches a simulated POST; the loser performs
no POST and cannot overwrite the winner's unresolved lifecycle state.
