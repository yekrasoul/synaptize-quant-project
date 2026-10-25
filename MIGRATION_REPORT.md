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

Before activation: obtain direct authenticated evidence for credential scope,
intended account/environment, and withdrawal exclusion; prove operational V2
Production Shadow parity; then route all production submission through V2 and
remove the legacy submit path in a separately reviewable change.

## Durable safety layer

This phase adds a V2-only durable execution repository, crash/restart recovery,
client-identity reconciliation, fresh-risk provider, venue-health risk input,
and idempotent accounting repository. This branch adds a schema-validated
`execution_record_v2` projection and read-only Production Shadow V2
planning/mapping parity. Pinned-CCXT offline signed-wire characterization is
now verified, but authenticated account/response parity and cutover from the
legacy submit path remain activation blockers; V2 remains fail-closed.
The full matrix is in [`BYBIT_CCXT_CUTOVER.md`](BYBIT_CCXT_CUTOVER.md).

The durable claim is filesystem compare-and-set, not an in-memory lock:
exclusive creation of a per-client identity claim file (`O_CREAT|O_EXCL`) plus
file and directory fsync. A spawned two-process test proves exactly one
contender reaches the simulated network boundary and restart cannot claim it
again.

The service-level race is also covered with two independent processes. One
receives submission ownership and reaches a simulated POST; the loser performs
no POST and cannot overwrite the winner's unresolved lifecycle state.

## Wire parity and writer audit

A no-network CCXT interception test now executes the real pinned Bybit CCXT
order constructor and captures the signed request at the exchange instance's
`fetch(url, method, headers, body)` boundary. It verifies the V5 method/path,
body, authentication-header structure, deterministic timestamp/receive-window,
quote-denominated BTC/USDT market-buy mapping, `marketUnit=quoteCoin`,
`isLeverage=0`, and canonical `orderLinkId`, while a socket guard proves zero
network I/O. This is offline characterization only; account/environment,
withdrawal, response, and operational Production Shadow evidence remain open.

The writer audit finds zero production-reachable network writers on committed
main: V2 is not wired into a CLI, and the historical canary writer is blocked
by checked-in execution disablement and the active kill switch. The legacy
writer remains in place until a future cutover makes V2 the sole reachable
writer; `one_reachable_production_submit_path` remains NOT VERIFIED.
