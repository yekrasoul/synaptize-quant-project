# Execution Platform V2 Migration Report

## Delivered in this phase

- Added CCXT as the contained CEX dependency and `VenueAdapter` boundary.
- Added canonical domain models, event envelope, OMS, execution states, risk,
  accounting gate, Adaptive DCA strategy seam, and a fail-closed runtime policy.
- Added `btc-dca execute` as a non-interactive V2 command. It safely blocks in
  the committed configuration before any credential or network access.

## Verification

Baseline was 585 unit tests. This phase adds 10 focused V2 tests; the complete
suite and canonical validation pass locally. Graphify update was attempted as
required by repository guidance, but the `graphify` executable is unavailable
in this checkout environment, so no graph output was claimed or changed.

## Not yet retired

The three legacy Bybit transports, legacy reconcilers, artifact ceremony, and
readiness projections are not removed in this commit. Removing them before
durable V2 submission/evidence parity would risk the exact safety controls this
migration is meant to preserve. They are explicitly legacy debt, not V2 owners.

## Remaining gates

Before activation: implement a durable V2 execution-record repository; add
captured Bybit/CCXT request-response parity fixtures; migrate current confirmed
ledger posting through accounting; then remove the legacy submit path and its
ceremony in a separately reviewable change.
