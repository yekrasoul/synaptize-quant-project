# BTC Adaptive DCA bridge

This directory is the canonical, scalable foundation for BTC Adaptive DCA V1.

Start with [the project specification](docs/PROJECT_SPEC.md). It defines source ownership, component boundaries, failures, security, versioning, and the roles of the Chats, GitHub, Telegram, and Bybit.

## Canonical assets

- [`config/strategy_v1.yaml`](config/strategy_v1.yaml): the single approved V1 rule definition.
- [`schemas/`](schemas): versioned JSON contracts for runtime artifacts.
- [`ledger/executions.jsonl`](ledger/executions.jsonl): reconciled execution facts; see [ledger rules](docs/LEDGER_RECONCILIATION.md).
- [`docs/MIGRATION_PLAN.md`](docs/MIGRATION_PLAN.md): controlled path from the current collector and workflow.

`latest.json` and `collect_bybit_spot.py` are legacy collector artifacts pending the migration plan. In particular, the current collector’s cross-exchange fallback is not compliant with V1’s Bybit-only primary market rule and must not be used to create a V1 decision. No purchase history or monthly state is duplicated in this README.

## Phase 1 status

Phase 1 establishes the contracts and data foundation only. It does not calculate or execute live orders. Future components must validate their inputs/outputs against these contracts and must read V1 thresholds from configuration rather than hard-code them.
