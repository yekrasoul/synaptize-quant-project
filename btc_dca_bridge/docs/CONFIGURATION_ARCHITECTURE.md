# Configuration architecture

Production behavior is defined by committed, versioned YAML and validated before a
shadow run begins.  Secrets are intentionally excluded.

| Owner | File | Examples |
|---|---|---|
| V1 strategy | `config/strategy_v1.yaml` | drawdown bands, Fear & Greed multipliers, monthly cap, rounding |
| Market operations | `config/market_data.yaml` | approved provider order, exact Spot identity, freshness, HTTP policy |
| Sentiment operations | `config/sentiment.yaml` | Alternative.me index, 36-hour freshness, HTTP policy |
| Runtime operations | `config/runtime.yaml` | London operational intent, UTC slot, minute alignment |
| Notification operations | `config/notifications.yaml` | Telegram retry policy and secret environment-variable names |
| Persistence operations | `config/persistence.yaml` | artifact root, Actions retention, SHA-256 policy |
| Research flags | `config/research.yaml` | disabled future-only analytics flags |

All operational files currently use `config_version: "1.0.0"`. Unsupported
versions, unknown fields, malformed values, unsafe paths, and unapproved source
identities fail closed. There are no ordinary environment overrides: committed
configuration is authoritative. Environment variables supply only the Telegram
secrets named by `notifications.yaml`. An explicit local CLI `--data-root` is a
developer/test output-location override; it does not alter persistence safety or
any production source, strategy, or execution policy.

## Safety boundaries

The following are code-enforced invariants, not switches: Spot only, no
derivatives/leverage/borrowing, exact Bybit `BTCUSDT` identity, no mixed-source
snapshot, no unvalidated source, no unapproved fallback, immutable no-overwrite
artifacts, no shadow-ledger mutation, no monthly-cap bypass, and no live order
execution. `runtime.live_execution_enabled` must be false and cannot enable a
nonexistent Phase 5 path.

The current provider order is Bybit public Spot API, then TradingView exact
`BYBIT:BTCUSDT`, then fail closed. Alternative.me is the sole sentiment source.
Changing an exchange, external symbol, provider, or sentiment source is a
**DATA-SOURCE CHANGE**: implement an adapter, define identity/provenance,
extend typed validation, add offline contract tests, review schemas, and approve
the change before its configuration can be accepted.

## Schedule and retention

`runtime.yaml` expresses the intent of 12:00 Europe/London and the fixed Phase 4
GitHub UTC slot (`0 11 * * *`). GitHub Actions cron cannot read YAML or follow
DST, so the workflow retains its explicit cron and tests ensure it matches the
runtime policy. Change a schedule by updating both, then reviewing DST impact.
Future choices include a new fixed UTC slot, seasonal maintenance, or a
timezone-aware scheduler; none is implemented here. Artifact retention values
are emitted by validated config to the upload steps; GitHub's job timeout is
static YAML and is tested against runtime config.

## Change classes

A timeout or notification retry change is a normal operational configuration
change. Monthly cap, drawdown bands, multiplier bands, and rounding are **LIVE
STRATEGY CHANGES** and remain solely in `strategy_v1.yaml`. RSI, moving averages,
funding, OI, ETF flows, macro and related flags are disabled research-only
concepts; they must write separate research reports/artifacts and cannot affect
V1 Decisions without an explicitly approved V2 strategy.

The intended future architecture is `MarketSnapshot -> Research/Analytics ->
ResearchArtifact`, separate from `MarketSnapshot + Sentiment + Ledger -> V1
Engine -> Decision`.
