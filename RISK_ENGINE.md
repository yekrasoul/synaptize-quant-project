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
