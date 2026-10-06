# Migration plan: collector and workflow

1. **Freeze the Phase 1 contracts.** Add contract validation to CI for configuration, ledger records, and every produced snapshot before any implementation move.
2. **Replace fallback behavior.** Refactor `collect_bybit_spot.py` into a collector module under `src/` that emits `MarketSnapshot` v1. It must be Bybit BTCUSDT Spot only; a Bybit failure writes a typed error artifact and exits non-zero, rather than falling back to another exchange.
3. **Move runtime output.** Write generated snapshots to `data/market_snapshots/` using immutable IDs; retain `latest.json` only as a compatibility pointer during a documented transition.
4. **Introduce the deterministic engine.** Implement and test a pure decision function that consumes the YAML config, a validated snapshot, and a `PortfolioState` derived from the JSONL ledger.
5. **Rewire GitHub Actions.** Separate collect, validate, decide, and notify steps. Grant write permission only if committing runtime artifacts remains necessary; otherwise publish artifacts without repository writes. Preserve a failed run’s typed error artifact.
6. **Add Telegram last.** Emit a `NotificationEvent` from an approved decision. Do not add exchange credentials or order submission.

Compatibility is complete only when the old collector and workflow are removed in the same change that proves equivalent valid Bybit output and provides an operator rollback path. Until then, `latest.json` remains legacy and must not be considered the canonical contract.
