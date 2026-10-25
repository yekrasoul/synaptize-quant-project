# Glossary

Each entry separates common usage from the meaning used in this project.

| Term | General meaning | Project-specific meaning |
|---|---|---|
| Spot | Direct trading of an asset rather than a derivative. | BTCUSDT Spot is the V1 market-data venue/product and intended execution product; derivatives and leverage are outside V1 scope. |
| Drawdown | Decline from a reference peak or high. | V1 uses percentage distance of current Bybit Spot price below its rolling 168-hour high to select configured allocation bands. |
| DCA | Repeated purchases over time to average entry price. | BTC Adaptive DCA V1 is a deterministic recommendation policy with a $10 daily core/minimum and $500 calendar-month cap; recommendations do not execute orders. |
| Monthly Cap | Maximum amount allowed in a calendar month. | V1's hard cap is $500 of active confirmed execution spend; it is a limit, not a target. |
| StrategyDecision | Output of a strategy's evaluation. | Immutable V1 recommendation/economic result; it is distinct from an OrderIntent or execution. |
| OrderIntent | Request describing a desired order before submission. | V2's immutable representation of a StrategyDecision translated into an intended BTC/USDT Spot order, subject to Risk and OMS controls. |
| OMS | Order Management System; tracks order lifecycle and identity. | V2 durable repository claims each client identity once and owns mutable lifecycle/recovery state. |
| EMS | Execution Management System; coordinates order submission and venue interaction. | V2 ExecutionService applies final risk/claim rules and calls an injected VenueAdapter; it is not currently permission to enable trading. |
| VenueAdapter | Boundary translating platform requests to venue-specific operations. | V2 adapter handles venue validation, submission, and reconciliation; the Bybit implementation uses CCXT. |
| Risk | Evaluation of exposure and execution constraints. | V2 Risk consumes current policy/account/spend/venue-health context and enforces the cap and safety constraints before an OMS claim. |
| Kill Switch | Control that blocks execution. | Independent authoritative execution control; checked-in state is active and execution remains blocked. |
| Circuit Breaker | Mechanism that suppresses calls to an unhealthy service. | `VenueCircuitBreaker` tracks venue health separately from the kill switch; Risk consumes its state. |
| SUBMITTED | A submission was acknowledged or sent, depending on system contract. | V2 lifecycle state after venue submission; it is not fill confirmation and does not authorize accounting. |
| AMBIGUOUS | Outcome cannot be determined after an operation. | V2 state for possible network write with unknown venue outcome; recovery reconciles by client identity and must not blindly resubmit. |
| CONFIRMED | A result has been verified against authoritative evidence. | Reconciled execution state supported by venue fill evidence; only this state may produce V2 accounting. |
| Reconciliation | Comparing records/evidence to establish the authoritative outcome. | Resolve Bybit order/fill state by canonical client identity; confirmed fills are the prerequisite for accounting and ledger projection. |
| Ledger | Durable record of financial events. | `btc_dca_bridge/ledger/executions.jsonl` is canonical append-only historical execution event history; active spend derives from correction/void projection. |
| Idempotency | Repeating an operation does not repeat its effect. | V2's durable per-client identity claim prevents another submission; confirmed accounting is create-once by execution identity. |
