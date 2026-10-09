# Deterministic shadow pipeline (Phase 3.8)

The shadow pipeline is read-only composition:

```text
Bybit BTCUSDT Spot
  → Binance BTCUSDT Spot fallback
  → KuCoin BTC-USDT Spot fallback
  → Alternative.me Crypto Fear & Greed
  → canonical reconciled execution ledger / UTC calendar month
  → unchanged deterministic V1 engine
  → immutable market, sentiment, and decision artifacts
  → immutable completed-run manifest
```

There is no live mode, order client, exchange credential, execution-ledger
write, or portfolio mutation. Phase 4 composes this API without changing it;
see `PRODUCTION_SHADOW.md` for scheduling, retention, and Telegram delivery.
`ShadowRunResult` is the structured programmatic result; console text is not an
integration contract.

## Run identity and time

The orchestrator receives one UTC acquisition instant and generates or accepts
one canonical `run_id`. The same ID is used for all component artifacts, their
receipts, the completion manifest, and `ShadowRunResult`. Market capture and
sentiment retrieval timestamps must equal that acquisition context. A default
run ID is deterministic from the injected timestamp. Phase 4 may additionally
provide a distinct UTC logical identity instant for a deterministic scheduled
slot while preserving the actual acquisition instant in artifacts. The UTC
acquisition month selects confirmed reconciled executions from
`ledger/executions.jsonl`; no duplicated monthly-spend state exists.

`started_at_utc` and `completed_at_utc` both record the single canonical logical run instant. This is intentional for deterministic replay; operational elapsed-time telemetry is outside the canonical strategy artifact.

## Completion, failure, and retry

Acquisition, ledger derivation, and Decision schema validation all finish before persistence begins. Persistence then publishes market, sentiment, and decision artifacts, followed last by `data/runs/YYYY/MM/DD/<run_id>.json`. Only a digest-verified, schema-valid run manifest with `status=completed` means success. A persistence failure can leave immutable component evidence, but never a misleading completed run.

Failures are stage-coded as `MARKET_DATA_FAILED`, `SENTIMENT_FAILED`, `LEDGER_FAILED`, `DECISION_FAILED`, or `PERSISTENCE_FAILED`, retaining the typed underlying cause. A completed duplicate raises `RUN_ALREADY_COMPLETED`. A partial run ID is not reused: retrying collides with existing immutable components and fails; the operator must choose a new run ID. Conflicting content can never replace existing bytes.

## CLI

Manual public read-only invocation:

```bash
uv run python -m btc_dca_bridge run --mode shadow
```

For deterministic diagnostics, add `--run-at 2026-10-07T12:00:00Z` and optionally a matching `--run-id`. Normal execution uses the approved public Spot source chain (Bybit -> Binance -> KuCoin), public Alternative.me, the local canonical ledger, and the runtime `data/` root. The selected venue must atomically supply both price and complete rolling 168-hour history. Failure of all approved venues exits non-zero.

Example successful ending:

```text
mode: SHADOW
run_id: run_20261007T120000Z_a1b2c3d4e5f6
market source: bybit_api
BTC price: $80000
rolling 7D high: $100000
drawdown: -20.0%
Fear & Greed: 35
base allocation: $75
sentiment multiplier: x1.3
calculated allocation: $98
monthly spent: $60
remaining budget: $440
FINAL PURCHASE: $98
SHADOW — BUY $98 BTC TODAY — NO ORDER EXECUTED
```

Example failure is emitted to stderr with non-zero status:

```json
{"status": "error", "error": "SENTIMENT_FAILED: sentiment snapshot acquisition failed"}
```
