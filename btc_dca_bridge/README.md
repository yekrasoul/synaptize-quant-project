# BTC Adaptive DCA bridge

This directory is the canonical, scalable foundation for BTC Adaptive DCA V1.

Start with [the project specification](docs/PROJECT_SPEC.md). It defines source ownership, component boundaries, failures, security, versioning, and the roles of the Chats, GitHub, Telegram, and Bybit.

## Canonical assets

- [`config/strategy_v1.yaml`](config/strategy_v1.yaml): the single approved V1 rule definition.
- [`schemas/`](schemas): versioned JSON contracts for runtime artifacts.
- [`ledger/executions.jsonl`](ledger/executions.jsonl): reconciled execution facts; see [ledger rules](docs/LEDGER_RECONCILIATION.md).
- [`docs/MIGRATION_PLAN.md`](docs/MIGRATION_PLAN.md): controlled path from the current collector and workflow.

`latest.json` and `collect_bybit_spot.py` are legacy collector artifacts pending the migration plan. In particular, the current collector’s cross-exchange fallback is not compliant with V1’s Bybit-only primary market rule and must not be used to create a V1 decision. No purchase history or monthly state is duplicated in this README.

## Offline core through Phase 3.1

Phase 2 adds a deterministic decision engine, read-only ledger validation, derived portfolio state, and an offline JSON CLI. Phase 3.1 ratifies `ROUND_HALF_UP` for exact nearest-dollar ties and evolves emitted `PortfolioState` artifacts from 1.0.0 to the additive 1.1.0 shape while retaining 1.0.0 validation compatibility. It still does not retrieve live sources, schedule work, notify users, or execute orders.

Install the explicitly declared dependencies from this directory (preferably in a virtual environment):

```console
python -m pip install -e .
```

Then use `python -m btc_dca_bridge validate`, `portfolio --month YYYY-MM`, or `calculate`. See [`docs/DECISION_ENGINE.md`](docs/DECISION_ENGINE.md) for contracts, calculation order, boundary behavior, and examples.

The production-oriented, read-only Bybit BTCUSDT Spot adapter and Phase 3.4
rolling-window rules are documented in
[`docs/BYBIT_SPOT_ADAPTER.md`](docs/BYBIT_SPOT_ADAPTER.md). It has no
alternate-exchange fallback and never submits orders.

Phase 3.4 adds fail-closed construction of a schema-valid `MarketSnapshot` from
that normalized ticker and complete hourly history. The exact 168-hour boundary
semantics and the deliberate non-hour-aligned limitation are documented in the
same adapter guide. It does not change any V1 allocation rule.

`uv.lock` is tracked. This is an application repository with a deterministic
test/runtime environment, so the lockfile pins the declared dependency graph
without changing dependency constraints in `pyproject.toml`.
