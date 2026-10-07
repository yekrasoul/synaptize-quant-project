# Phase 5.8 Pre-Authorization Runbook

This is a read-only gate. It does not create approval, authorize a real-money order,
submit an order, or schedule execution.

1. Verify the exact repository commit and clean deployment state.
2. Run `btc-dca production-connectivity`.
3. Run `btc-dca production-readiness`.
4. Collect and verify fresh production evidence.
5. Run `btc-dca preauthorization-status`.
6. Inspect Spot availability provenance and quote-unit limit evidence.
7. Stop at `BLOCKED`, `READY_FOR_OPERATOR_PREPARATION`, or
   `READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION`.

The final state is only a technical precondition. It is never `AUTHORIZED`, `EXECUTE`,
or `BUY`. Real-money authorization remains false and requires a separate explicit
authorization boundary.

Phase 5.8 currently stops at `QUOTE_UNIT_MAX_NOT_EXPOSED`. No `/v5/order/pre-check`
request is made because its official contract does not make it a suitable Spot
quote-limit proof and it is a POST endpoint.
