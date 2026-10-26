# Risk Engine

`RiskEngine` is the authoritative V2 platform safety evaluator. It blocks an
intent for: kill switch, disabled runtime policy, non-spot market, leverage,
borrowing, stale data, account identity mismatch, unresolved or duplicate
execution, insufficient allocated capital, and a monthly spend exceeding $500.

Native minimum notional and precision belong to the Venue Adapter. The DCA
strategy still computes V1 allocation but no longer owns the platform hard cap.

The one current-generation operational control is
`btc_dca_bridge/config/runtime_execution_policy.yaml`. It owns the kill switch
and execution-enabled state and ships fail-closed.

The execution service receives a `FreshRiskContextProvider`; it fetches a new
context immediately before the durable submission claim rather than reusing
preflight state. Venue health is a separate circuit-breaker input (`healthy`,
`degraded`, or `unavailable`); it never replaces the authoritative kill switch.
Venue availability is owned outside Risk by `VenueCircuitBreaker`. Its current
state is supplied through `VenueHealthRiskContextProvider` at the final
pre-submit Risk read. Breaker state deliberately restarts UNAVAILABLE until a
successful health probe; it never silently resets to HEALTHY. This is separate
from the authoritative kill switch.


## Engine profile — CURRENT CANONICAL

**Purpose and ownership.** `RiskEngine.evaluate(OrderIntent, RiskContext) -> RiskAssessment` is a deterministic, in-memory platform safety decision. It does not perform network I/O, place orders, read the venue, persist order state, or compute strategy allocations. `RiskAssessment` carries `APPROVED`/`BLOCKED`, reason codes, and remaining monthly allocation. An `APPROVED` assessment is **not** production activation authorization.

**Inputs.** `OrderIntent` identifies the decision, instrument, side, quote quantity, and linked market/sentiment evidence. `RiskContext` supplies calendar month, confirmed spend, cap, execution-enabled flag, kill switch, market freshness, account identity and expected fingerprint, quote balance, unresolved/duplicate indicators, venue health, approved venue/market, leverage/borrowing flags, and `MarketEvidenceBinding`. The binding checks decision/snapshot IDs and hashes, Fear & Greed and final purchase amount against the intent.

**Safety / fail closed.** `evaluate` collects reason codes rather than short-circuiting: `KILL_SWITCH_ACTIVE`, `EXECUTION_POLICY_DISABLED`, `VENUE_HEALTH_NOT_HEALTHY`, `VENUE_NOT_ALLOWED`, `MARKET_NOT_ALLOWED`, `MARKET_NOT_SPOT`, `SIDE_NOT_ALLOWED`, `LEVERAGE_PROHIBITED`, `BORROWING_PROHIBITED`, `STALE_MARKET_DATA`, `MARKET_DATA_EVIDENCE_UNVERIFIED`, `ACCOUNT_IDENTITY_MISMATCH`, `UNRESOLVED_EXECUTION`, `DUPLICATE_EXECUTION_IDENTITY`, `INSUFFICIENT_ALLOCATED_CAPITAL`, `INVALID_MONTHLY_CAP_POLICY`, and `MONTHLY_CAP_EXCEEDED`. Consult the [evaluator source](btc_dca_bridge/src/btc_dca_bridge/risk/engine.py) for exact semantics; monetary values and enabled flags belong to [canonical runtime policy](btc_dca_bridge/config/runtime_execution_policy.yaml), not this profile.

**Boundaries.** Strategy owns economic recommendation; a context provider constructs fresh Risk inputs; `VenueCircuitBreaker` owns venue-health state; `VenueAdapter` owns native precision/notional validation and exchange calls; durable OMS owns submission identity/lifecycle; EMS coordinates the pre-submit boundary and single venue submission; reconciliation determines fill outcome; accounting records only confirmed reconciled economic impact. A post-network uncertainty must not be treated as a safe retry.

**Primary references.** [Risk model and evaluator](btc_dca_bridge/src/btc_dca_bridge/risk/engine.py) · [shared pre-submit planning](btc_dca_bridge/src/btc_dca_bridge/execution_v2/pre_submit.py) · [execution service](btc_dca_bridge/src/btc_dca_bridge/execution_v2/service.py) · [V2 architecture](ARCHITECTURE_V2.md) · [activation checklist](btc_dca_bridge/config/v2_activation_checklist.yaml). Focused checks include [V2 market provenance](btc_dca_bridge/tests/test_v2_market_provenance.py) and [V2 composition root](btc_dca_bridge/tests/test_v2_composition_root.py); test and CI results must be checked against the relevant commit.

## TARGET and verification limits

**TARGET:** production use must have fresh authenticated account/venue evidence and pass every independent execution gate. **NOT_VERIFIED here:** live account identity, credential withdrawal exclusion, actual production execution reachability, and fully operational V2 shadow parity. This Markdown profile is navigation/education, not an Engine Registry, executable contract, operational health check, or approval to trade.

For newcomers, begin with [glossary](docs/reference/glossary.md) (OrderIntent, kill switch, AMBIGUOUS, reconciliation) and then inspect the cited models/tests. Additional concepts worth documenting only when needed are `RiskContext`, `MarketEvidenceBinding`, fresh context providers, and durable submission claim.
