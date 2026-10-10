# Phase 5.5A — Bybit Spot live transport preparation

Phase 5.5A/5.5B adds the authenticated Bybit V5 Spot transport and the
guarded submission boundary for the future BTCUSDT execution path. The
submission method is fail-closed under the current production policy.

## Safety boundary

The checked-in execution policy remains unchanged:

```yaml
live_execution_enabled: false
kill_switch: true
explicit_live_approval_required: true
monthly_cap_usd: 500
market: Bybit Spot BTCUSDT
order_submission: not_implemented
```

`BybitSpotRestTransport` loads `BYBIT_API_KEY` and `BYBIT_API_SECRET` from
the environment and fails closed when either is absent. Secrets are kept out
of representations, errors, normalized responses, tests, and documentation.

The transport permits named HTTPS GET operations for authenticated read-only
evidence and exactly one guarded POST path, `/v5/order/create`. It signs V5
query requests using:

```text
timestamp + api_key + recv_window + queryString
```

It also signs pure JSON request preparation for the exact Spot BTCUSDT Market
Buy contract. `submit_order()` independently re-reads the complete execution
gate state immediately before the POST and uses a one-shot idempotency guard.
Acknowledgement is submission acceptance only; it is not fill confirmation,
and reconciliation remains separate. No amend or cancel path exists.

## Exact future market shape

The prepared model is restricted to:

```json
{
  "category": "spot",
  "symbol": "BTCUSDT",
  "side": "Buy",
  "orderType": "Market",
  "marketUnit": "quoteCoin",
  "isLeverage": 0,
  "orderFilter": "Order"
}
```

The quote amount is supplied by the already-approved V1 intent. No rounding,
wallet-driven resizing, split order, retry, leverage, derivatives, or
alternate market is supported. Client order identity uses the existing
deterministic V1 identity inputs and request fingerprints are SHA-256 over
canonical JSON.

## Submission guard chain

The POST is refused unless all of these are true, in this order:

```text
live_execution_enabled == true
kill_switch == false
order_submission == implemented
Production Enablement == ENABLED
Authorization == AUTHORIZED
Activation == ACTIVATED
Execution == ALLOWED
market == Bybit Spot BTCUSDT
```

The current checked-in state fails at the first gate and therefore produces
zero POST calls and no ledger mutation. Repeated attempts with the same
request fingerprint are blocked and ambiguous transport outcomes require
reconciliation before any retry.

## Remaining blocker

Before a real order can be submitted, the existing governed
ProductionEnablement → authorization → activation → Gate A/B/C boundary must
be explicitly enabled by a future phase. Current defaults remain blocked,
credentials are not added to CI, and no Bybit order was submitted in Phase
5.5B.
