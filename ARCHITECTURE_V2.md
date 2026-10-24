# Architecture V2

Baseline: `4c6a94ebd8f78bdcab0043fa095fdcad604d2fb1`.

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

The V2 cutover decision and remaining legacy boundaries are in
[`BYBIT_CCXT_CUTOVER.md`](BYBIT_CCXT_CUTOVER.md).
