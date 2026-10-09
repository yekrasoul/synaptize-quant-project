# Legacy market-path retirement (Phase 3.9)

Phase 3.9 removed the obsolete root-level `collect_bybit_spot.py` script and its tracked mutable `latest.json` output. The script attempted Bybit, then Binance, then KuCoin and emitted an unversioned competing snapshot shape. It bypassed the canonical adapters, schema validation, typed failure policy, immutable storage, and completed-run manifest.

The transition-era `docs/MIGRATION_PLAN.md` was also removed because every relevant migration step is complete and its instructions to retain a mutable compatibility pointer were no longer safe or accurate. Git history preserves all three retired files if historical inspection is needed. No legacy runtime entrypoint remains, so there is no compatibility shim or silent redirect.

## Canonical replacements

- BTC market data: Bybit BTCUSDT Spot direct, then TradingView exact `BYBIT:BTCUSDT` Spot, otherwise fail closed.
- Sentiment: Alternative.me Crypto Fear & Greed only.
- Monthly spend: derived from confirmed reconciled records in `ledger/executions.jsonl`.
- Strategy: unchanged deterministic V1 engine resolved from the repository bootstrap manifest, including the approved content digest.
- Run state: immutable market, sentiment, and decision artifacts plus the final schema-valid completed-run manifest.
- Operator entrypoint: `uv run python -m btc_dca_bridge run --mode shadow`.

No tracked cron or GitHub Actions workflow invoked the legacy collector at retirement time. Nothing in `src/btc_dca_bridge/` imported it. Production and shadow code must not reintroduce alternate exchanges, generic BTC prices, derivative symbols, mutable latest-state files, or imports from historical collector code. Decision artifacts remain recommendations and are never appended to the execution ledger.
