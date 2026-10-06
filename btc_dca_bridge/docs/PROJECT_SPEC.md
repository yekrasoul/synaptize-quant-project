# BTC Adaptive DCA — canonical project specification

## 1. Purpose and scope

This repository provides the auditable foundation for BTC Adaptive DCA V1: collect valid market data, produce a deterministic purchase recommendation, reconcile confirmed spot purchases, and notify an operator. It is a disciplined six-month accumulation workflow, not a price-prediction or trading system.

Phase 1 establishes contracts, configuration, data ownership, and migration boundaries. It intentionally contains **no live-order integration**, no custody logic, no leverage, and no V2 indicator or rule changes.

## 2. Canonical sources of truth

| Concern | Canonical source | Rule |
|---|---|---|
| V1 allocation rules | `config/strategy_v1.yaml` | One definition; code and automation read it rather than duplicate thresholds. |
| Contract shapes | `schemas/*.schema.json` | Versioned JSON Schema contracts. |
| Executed-purchase history | `ledger/executions.jsonl` | Reconciled facts only; monthly spend is derived. |
| Market snapshot | validated `MarketSnapshot` generated from Bybit BTC/USDT Spot | Price and 168h high come from the same Bybit Spot source. |
| Human-facing documentation | this document and other `docs/` files | Documents policy; it never stores operational state. |

Crypto Fear & Greed is an independent, secondary sentiment input. It may adjust a V1 allocation only through the configured multiplier; derivatives, ETF, macro, funding, OI, and liquidations are contextual research only.

## 3. Component boundaries

```text
Bybit Spot ──> collector ──> MarketSnapshot ──> deterministic decision engine ──> Decision
F&G source ────────────────┘                         ▲                         │
                                                       │                         ├──> Telegram notification
Execution evidence ─> reconciliation ─> Execution ledger ─> PortfolioState ────┘
```

- **Collector:** obtains and validates data; it never changes allocation rules or sends an order.
- **Decision engine:** pure, deterministic transformation of a valid snapshot, the strategy config, and derived monthly state. It has no network, Telegram, or exchange-order dependency.
- **Ledger/reconciliation:** stores only confirmed executions and exposes derived `PortfolioState`.
- **Notifier:** transports an already-created event to Telegram and records delivery status. It must not be treated as execution confirmation.
- **Orchestrator:** assigns run/correlation IDs, invokes components, persists artifacts, and applies failure policy.

## 4. V1 decision invariants

The canonical config preserves the approved V1 rules exactly: $10 daily core/minimum, $500 hard calendar-month cap, 168-hour rolling-high drawdown bands, Fear & Greed multipliers, nearest-dollar rounding, and no carry-forward. To remove ambiguous prose boundaries, exact -5%, -10%, -15%, and -25% values resolve to the less-severe band (`>=` its lower limit); values below -25% use $100. The cap is a limit, not a target. A remaining budget below $10 produces $0 and a cap-reached outcome.

The calculation is recommendation-only. An operator must execute any spot purchase separately and reconcile it after confirmation. No module may submit Bybit orders in V1.

## 5. Failure policy

- Missing, stale, contradictory, incomplete, or non-Bybit-Spot market data yields `data_unavailable`; no allocation is fabricated.
- A collector must never silently substitute Binance, KuCoin, derivatives, an index price, or a mixed-source 7D high.
- Invalid Fear & Greed data also yields `data_unavailable`; it is a required V1 input for a final recommendation.
- A schema validation, ledger parse, or monthly-cap derivation failure stops the run before notification.
- Notification delivery failure does not change a decision or create an execution; it is recorded as failed and can be retried idempotently.
- A duplicate `execution_id` or conflicting reconciliation evidence is quarantined for manual review, never auto-merged.

## 6. Security and operational boundaries

API credentials, Telegram bot tokens, and exchange credentials are secrets: use GitHub Actions secrets or the deployment environment only; never commit them or include them in snapshots/logs. GitHub Actions should have least-privilege permissions. Bybit access is read-only in this phase. Human confirmation remains the boundary between a recommendation and a purchase.

## 7. Versioning and compatibility

Strategy and contract versions are independent semantic versions. A V1 rule change is not an in-place edit: create a proposed version, document migration impact, obtain explicit approval, and retain old artifacts with their original version. New optional schema fields can be minor-compatible; removals, changed meanings, or stricter required fields require a major version. IDs (`snapshot_id`, `decision_id`, `execution_id`, `event_id`) are immutable and correlation IDs connect a run’s artifacts.

## 8. Operating roles

| System/channel | Responsibility | Authority boundary |
|---|---|---|
| Chat 01 | strategy mandate and approved operating rules | approves changes to the strategy mandate; not a runtime data store |
| Chat 02 | daily market/sentiment research and calculation input review | research only; cannot override config |
| Chat 03 | portfolio and budget reconciliation | authoritative human evidence for ledger entries |
| Chat 04 | architecture, backtest, and controlled evolution | proposes/validates architecture; cannot silently change live V1 |
| GitHub | versioned code/config/contracts/automation and audit history | no secret or manual-execution substitute |
| Telegram | delivery channel for notification events | notification is not order confirmation |
| Bybit | required BTCUSDT Spot market-data source | no trading API use in Phase 1 |

## 9. Repository layout

```text
btc_dca_bridge/
  config/       # canonical strategy configuration
  schemas/      # versioned data contracts
  data/         # runtime artifacts; not canonical execution history
  ledger/       # reconciled append-only execution facts
  src/          # future application modules
  tests/        # contract and invariant checks
  docs/         # architecture, migration, reconciliation documentation
```
