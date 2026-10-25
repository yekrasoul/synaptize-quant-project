# Architecture V2

Baseline: `ca6fb1b250ce7c1c248c14db1c6e4992acf7946e`.

The current-generation dependency direction is:

`research -> strategy -> risk -> OrderIntent -> OMS -> EMS/Venue -> reconciliation -> accounting`.

`core` owns canonical values and the execution state machine. `strategies` own
economic decisions; Adaptive DCA V1 wraps the existing pure calculator without
changing its allocation rules. `risk` owns the $500 hard cap and platform
safety. `venues` own CCXT and venue mapping. `accounting` accepts only
reconciled execution truth. The legacy Phase 5.x modules remain read-only
compatibility/reference code until their behavior is fully characterized.

Architecture tests prevent Core/Risk from importing venue or CLI code and keep
CCXT confined to `venues/ccxt`. The durable OMS has one mutable lifecycle
record per client identity; `execution_record_v2` is its schema-validated,
immutable, secret-free audit projection, not a second state owner. Production
Shadow calls the V2 pre-network planning path and cannot create a claim or POST.

Venue health is owned by `VenueCircuitBreaker`, outside Risk; Risk consumes its
current state while the kill switch remains a separate authoritative control.
Similarly, confirmed V2 accounting persistence is not yet consumed by the
legacy monthly-spend projection, so that integration remains NOT VERIFIED.

The V2 cutover decision and remaining legacy boundaries are in
[`BYBIT_CCXT_CUTOVER.md`](BYBIT_CCXT_CUTOVER.md).

## Canonical target composition root

`execution_v2.composition.build_v2_execution_runtime` is the single
dependency-injected TARGET_V2 composition root. It assembles the canonical
V1 decision input, V2 `OrderIntent`, fresh policy/account/spend/venue-health
context, Risk, OMS, `ExecutionService`, an injected `VenueAdapter`, and
confirmed-only accounting. It does not import or construct CCXT; infrastructure
supplies the adapter and authenticated context explicitly.

The `btc-dca execute` command remains fail-closed before this runtime can be
constructed while the committed policy and activation checklist are blocked.
Production Shadow can receive the same runtime and call its shared
pre-submit planner, which stops before OMS claim and `submit_once`. The
scheduled shadow path supplies no authenticated V2 runtime when that context is
unavailable, so `production_shadow_v2_pre_network` remains
`PARTIALLY_VERIFIED`.
