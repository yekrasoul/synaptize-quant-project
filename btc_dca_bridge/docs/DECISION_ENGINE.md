# Deterministic V1 decision engine

## Scope and boundaries

The Phase 2 engine is an offline, deterministic transformation. It does not retrieve market data, contact an exchange, submit an order, schedule work, or send a notification. The strategy engine is pure business logic; ledger reading, portfolio derivation, and the CLI are separate adapters.

`config/strategy_v1.yaml` is the sole source of strategy constants. This document describes how the engine interprets that configuration and does not supersede it.

## Inputs

The calculation accepts:

- a schema-valid `MarketSnapshot` containing Bybit BTCUSDT Spot price and rolling 168-hour high from the same source;
- an integer Fear & Greed value from 0 through 100;
- confirmed calendar-month spend from the canonical ledger, in the inclusive range from zero through the configured cap; and
- a validated strategy configuration with the expected V1 identity and version.

Current price and rolling high must both be positive. Current price above the stated rolling high is contradictory and is rejected. Inputs are never clamped or silently repaired.

## Output

The output is a `StrategyDecision` conforming to `schemas/decision.schema.json`. Its ID is a stable hash of all calculation inputs and strategy identity, so identical input produces identical output. `created_at_utc` is inherited from the snapshot rather than read from the system clock.

The engine returns recommendations only. A positive `final_purchase_usd` is not an execution or exchange fill.

## Calculation sequence

1. Validate the snapshot, sentiment, spend, strategy identity, and strategy structure.
2. Calculate drawdown as `(price - rolling high) / rolling high * 100` using decimal arithmetic.
3. Select the first matching drawdown band from the canonical configuration.
4. Select the inclusive Fear & Greed band from the canonical configuration.
5. Multiply base allocation by the sentiment multiplier.
6. Round to the nearest whole USD. Exact half-dollar ties use conventional decimal half-up rounding.
7. Restore the configured minimum if sentiment reduced the rounded amount below it.
8. Calculate exact remaining budget as cap minus confirmed spend.
9. If remaining budget is below the minimum, return zero with `monthly_cap_reached`.
10. If calculated allocation fits, return it unchanged. Otherwise return `floor(remaining budget)`, ensuring fractional spend can never push the month above the hard cap.

## Boundary semantics

Drawdown bands are evaluated in canonical list order. Zero through exactly -5% uses the first band. Exact -10%, -15%, and -25% values stay in their respective less-severe bands; only values immediately below each boundary enter the next band. Values below -25% enter the final band.

Fear & Greed bands are inclusive at both ends. Consequently the transitions are 20/21, 35/36, 50/51, 65/66, 75/76, and 90/91, exactly as represented by adjacent `max_index` and `min_index` values in the configuration.

## Validation and failure behavior

Malformed YAML, incomplete or overlapping bands, unsupported policy values, and unexpected strategy identity/version raise a configuration error. Invalid snapshot fields, non-positive prices/highs, contradictory price/high pairs, sentiment outside 0–100, and invalid monthly spend raise an input error. Malformed JSONL, schema-invalid executions, and duplicate execution IDs raise a ledger error. No failure path fabricates a decision or modifies the ledger.

All five JSON Schemas are parsed and meta-schema checked. Snapshot, execution, generated decision, and derived portfolio-state boundary artifacts are validated with JSON Schema Draft 2020-12 and format checking.

## CLI

Install the declared project dependencies, then run from this directory:

```console
python -m btc_dca_bridge validate
python -m btc_dca_bridge portfolio --month 2026-10
python -m btc_dca_bridge calculate --price 80000 --high-7d 90000 --fear-greed 30 --monthly-spent 220
```

Commands emit JSON to standard output. Errors emit a JSON object to standard error and return a non-zero status. `calculate` uses fixed valid CLI snapshot metadata unless `--snapshot-id` or `--captured-at` is supplied; those metadata defaults keep repeated calculations reproducible and perform no retrieval.
