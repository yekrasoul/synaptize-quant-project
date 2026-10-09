# BTC Adaptive DCA bridge

This directory is the canonical, scalable foundation for BTC Adaptive DCA V1.

Start with [the project specification](docs/PROJECT_SPEC.md). It defines source ownership, component boundaries, failures, security, versioning, and the roles of the Chats, GitHub, Telegram, and Bybit.

## Canonical assets

- [`config/strategy_v1.yaml`](config/strategy_v1.yaml): the single approved V1 rule definition.
- [`schemas/`](schemas): versioned JSON contracts for runtime artifacts.
- [`ledger/executions.jsonl`](ledger/executions.jsonl): reconciled execution facts; see [ledger rules](docs/LEDGER_RECONCILIATION.md).
- [`docs/SHADOW_PIPELINE.md`](docs/SHADOW_PIPELINE.md): canonical read-only Phase 3 composition.
- [`docs/PRODUCTION_SHADOW.md`](docs/PRODUCTION_SHADOW.md): Phase 4 schedule, retention, and Telegram operations.
- [`docs/LEGACY_RETIREMENT.md`](docs/LEGACY_RETIREMENT.md): retired collector and mutable-output history.
- [`docs/PROJECT_CHAT_CONTRACT.md`](docs/PROJECT_CHAT_CONTRACT.md): mandatory cross-interface ledger synchronization contract for every BTC DCA project chat.

The legacy cross-exchange collector and mutable `latest.json` output have been
removed. They are not compatibility entrypoints and must not be recreated or
used as V1 inputs. No purchase history or monthly state is duplicated here.

## Canonical Phase 3 shadow pipeline

The production-capable read-only shadow path uses the approved Spot source order:
Bybit BTCUSDT Spot, then Binance BTCUSDT Spot, then KuCoin BTC-USDT Spot. Each
candidate venue must independently provide both the current price and the
complete rolling 168-hour high; values are never mixed across venues. If all
approved sources fail, the run fails closed. Alternative.me is the sole
sentiment source. Calendar-month confirmed spend is derived from the canonical
execution ledger through the active reconciliation projection. The deterministic
V1 engine produces a recommendation and immutable audit artifacts. Normal shadow
operation never submits an order or treats a notification as execution evidence.

Install the explicitly declared dependencies from this directory (preferably in a virtual environment):

```console
python -m pip install -e .
```

Then use `python -m btc_dca_bridge validate`, `portfolio --month YYYY-MM`,
`calculate`, or `run --mode shadow`. See
[`docs/DECISION_ENGINE.md`](docs/DECISION_ENGINE.md) for calculation rules and
[`docs/SHADOW_PIPELINE.md`](docs/SHADOW_PIPELINE.md) for composition and failure
semantics.

The production-oriented Bybit BTCUSDT Spot adapter remains the primary source.
Approved fallback orchestration is implemented separately in
`market_data/approved_spot.py` and is fixed to Bybit -> Binance -> KuCoin.
The exact rolling-window rules are documented in
[`docs/BYBIT_SPOT_ADAPTER.md`](docs/BYBIT_SPOT_ADAPTER.md). Market-data
adapters never submit orders.

Phase 3.4 adds fail-closed construction of a schema-valid `MarketSnapshot` from
that normalized ticker and complete hourly history. The exact 168-hour boundary
semantics and the deliberate non-hour-aligned limitation are documented in the
same adapter guide. It does not change any V1 allocation rule.

`uv.lock` is tracked. This is an application repository with a deterministic
test/runtime environment, so the lockfile pins the declared dependency graph
without changing dependency constraints in `pyproject.toml`.
