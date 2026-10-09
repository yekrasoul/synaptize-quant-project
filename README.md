# synaptize-quant-project

Versioned foundation for the BTC Adaptive DCA strategy. The canonical implementation lives in [`btc_dca_bridge/`](btc_dca_bridge/README.md).

It is recommendation-first and fail-closed. A narrowly scoped guarded execution
engine exists for future explicitly authorized operation, but checked-in
production defaults disable it and the external quote-limit blocker prevents
production execution today.

## Architecture and operations index

The repository keeps the V1 decision separate from all order and execution state:

```text
Decision != OrderIntent != Approval != SubmissionAttempt != Execution
```

Only confirmed, authoritative fills may create a final execution and update the
canonical ledger. Decisions, canaries, approvals, attempts, status snapshots,
and notifications are not spend records.

- [Repository bootstrap and active strategy declaration](btc_dca_bridge/config/project_manifest.yaml), [V1 strategy config](btc_dca_bridge/config/strategy_v1.yaml), [market data](btc_dca_bridge/config/market_data.yaml), [runtime](btc_dca_bridge/config/runtime.yaml), and [execution safety defaults](btc_dca_bridge/config/execution.yaml)
- [Canonical ledger](btc_dca_bridge/ledger/executions.jsonl), [Ledger Gateway contract](btc_dca_bridge/docs/LEDGER_GATEWAY.md), and [cross-interface chat contract](btc_dca_bridge/docs/PROJECT_CHAT_CONTRACT.md)
- [Sentiment config](btc_dca_bridge/config/sentiment.yaml) and [Project Instructions governance template](btc_dca_bridge/docs/PROJECT_INSTRUCTIONS_TEMPLATE.md)
- [Canary preparation](btc_dca_bridge/src/btc_dca_bridge/canary.py) and [persisted manual approval/live-order boundary](btc_dca_bridge/src/btc_dca_bridge/live_order.py)
- [Live-order safety boundary](btc_dca_bridge/src/btc_dca_bridge/live_order.py)
- [Production readiness](btc_dca_bridge/src/btc_dca_bridge/readiness.py) and [production evidence](btc_dca_bridge/src/btc_dca_bridge/production_evidence.py)
- [Blocked-production contract model](btc_dca_bridge/src/btc_dca_bridge/blocked_production.py)
- [Post-submission reconciliation](btc_dca_bridge/src/btc_dca_bridge/private_bybit.py)
- [Operations and artifact audit](btc_dca_bridge/src/btc_dca_bridge/operations.py)
- [Production status snapshots and alert recovery](btc_dca_bridge/src/btc_dca_bridge/production_status.py)
- [Production operations runbook](PRODUCTION_OPERATIONS_RUNBOOK.md)
- [Phase 6.1 final closeout](PHASE_6_1_FINAL_CLOSEOUT.md)
- [Current project status](PROJECT_STATUS.md)
