# synaptize-quant-project

Versioned foundation for the BTC Adaptive DCA strategy. The canonical implementation lives in [`btc_dca_bridge/`](btc_dca_bridge/README.md).

Normal V1 operation is recommendation-first. V2 execution routing is gated and
production activation remains blocked by incomplete readiness evidence.

For current architecture, readiness, and documentation navigation, start with
the [Docs-as-Code entrypoint](docs/START_HERE.md) and
[project baseline](docs/PROJECT_BASELINE.md). Current evidence shows activation
is blocked by incomplete account/credential evidence and operational Shadow
parity, not the previously reported quote-limit blocker.

## Architecture and operations index

The repository separates strategy decisions, order intents, risk assessments,
and confirmed execution records:

```text
StrategyDecision != OrderIntent != RiskAssessment != ExecutionRecord
```

Only venue-confirmed fills established through reconciliation affect accounting.
Submission acknowledgements, decisions, historical approvals, shadow runs,
and notifications are not confirmed spend. The single reachable production
submission route is the gated V2 `execute` path; legacy `canary-execute` is retired.

- [Repository bootstrap and active strategy declaration](btc_dca_bridge/config/project_manifest.yaml), [V1 strategy config](btc_dca_bridge/config/strategy_v1.yaml), [market data](btc_dca_bridge/config/market_data.yaml), [V1 runtime](btc_dca_bridge/config/runtime.yaml), and [V1 execution defaults](btc_dca_bridge/config/execution.yaml)
- [V2 execution runtime policy](btc_dca_bridge/config/runtime_execution_policy.yaml), [V2 activation checklist](btc_dca_bridge/config/v2_activation_checklist.yaml), and [V2 cutover status](BYBIT_CCXT_CUTOVER.md)
- [Canonical ledger](btc_dca_bridge/ledger/executions.jsonl), [Ledger Gateway contract](btc_dca_bridge/docs/LEDGER_GATEWAY.md), and [cross-interface chat contract](btc_dca_bridge/docs/PROJECT_CHAT_CONTRACT.md)
- [Sentiment config](btc_dca_bridge/config/sentiment.yaml) and [Project Instructions governance template](btc_dca_bridge/docs/PROJECT_INSTRUCTIONS_TEMPLATE.md)
- Historical Phase 5 [canary preparation](btc_dca_bridge/src/btc_dca_bridge/canary.py) and [legacy live-order boundary](btc_dca_bridge/src/btc_dca_bridge/live_order.py) remain reference/compatibility code, not a reachable production writer.
- [Production readiness](btc_dca_bridge/src/btc_dca_bridge/readiness.py) and [production evidence](btc_dca_bridge/src/btc_dca_bridge/production_evidence.py)
- [Blocked-production contract model](btc_dca_bridge/src/btc_dca_bridge/blocked_production.py)
- [Post-submission reconciliation](btc_dca_bridge/src/btc_dca_bridge/private_bybit.py)
- [Operations and artifact audit](btc_dca_bridge/src/btc_dca_bridge/operations.py)
- [Production status snapshots and alert recovery](btc_dca_bridge/src/btc_dca_bridge/production_status.py)
- [Production operations runbook](PRODUCTION_OPERATIONS_RUNBOOK.md)
- [Phase 6.1 final closeout](PHASE_6_1_FINAL_CLOSEOUT.md)
- [Current project status](PROJECT_STATUS.md)
