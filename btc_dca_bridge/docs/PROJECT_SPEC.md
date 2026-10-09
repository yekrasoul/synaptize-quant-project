# BTC Adaptive DCA — canonical project specification

## 1. Purpose and scope

This repository provides the auditable foundation for BTC Adaptive DCA V1: collect valid market data, produce a deterministic purchase recommendation, reconcile confirmed spot purchases, and notify an operator. It is a disciplined six-month accumulation workflow, not a price-prediction or trading system.

The repository contains the production-shadow path plus guarded Phase 5 canary/live-order components. Normal V1 operation remains recommendation-only: checked-in execution configuration keeps live execution disabled, the kill switch enabled, and order submission unavailable. No standing authorization, custody logic, leverage, or V2 indicator/rule change is implied by the presence of those guarded components.

## 2. Canonical sources of truth

All ChatGPT/project interfaces must also follow [`PROJECT_CHAT_CONTRACT.md`](PROJECT_CHAT_CONTRACT.md), which defines mandatory cross-chat ledger READ/WRITE/CORRECT/CANCEL behavior.

| Concern | Canonical source | Rule |
|---|---|---|
| V1 allocation rules | `config/strategy_v1.yaml` | One definition; code and automation read it rather than duplicate thresholds. |
| Contract shapes | `schemas/*.schema.json` | Versioned JSON Schema contracts. |
| Executed-purchase history | `ledger/executions.jsonl` via `LedgerGateway` | Single canonical cross-interface ledger; reconciled facts only; corrections/voids are append-only events; monthly spend is derived. |
| Market snapshot | validated `MarketSnapshot` from the approved Spot source chain | Source order is Bybit BTCUSDT Spot -> Binance BTCUSDT Spot -> KuCoin BTC-USDT Spot. Current price and rolling 168h high must come atomically from the same selected venue. If all approved venues fail validation, no decision is produced. |
| Sentiment snapshot | validated Alternative.me Crypto Fear & Greed observation | One independent public source; no substitute sentiment signal. |
| Completed run state | immutable component artifacts plus schema-valid shadow-run manifest | A run is complete only when its final digest-verified manifest exists. Mutable `latest.json` state is prohibited. |
| Production-shadow retention | GitHub Actions artifact named by `run_id` | Completed JSON artifacts and digest sidecars are retained together for 90 days; this is durable operational retention, not permanent archival storage. |
| Operator notification | Telegram Bot API from the structured completed or failed run result | Plain-text delivery occurs after retention; it never creates execution evidence. |
| Human-facing documentation | this document and other `docs/` files | Documents policy; it never stores operational state. |

Crypto Fear & Greed is an independent, secondary sentiment input. It may adjust a V1 allocation only through the configured multiplier; derivatives, ETF, macro, funding, OI, and liquidations are contextual research only.

The former standalone cross-exchange collector and mutable `latest.json` output
were retired in Phase 3.9. They are not valid compatibility or recovery paths.

## 3. Component boundaries

```text
Bybit Spot ─┐
Binance Spot├─> ordered atomic source selector ─> MarketSnapshot ─> deterministic V1 engine ─> Decision
KuCoin Spot ┘                                      ▲                                  │
Alternative.me Fear & Greed ──────────────────────┘                                  ├─> Telegram / operator output
Execution evidence ─> LedgerGateway ─> execution ledger ─> PortfolioState ───────────┘
```

- **Collector:** obtains and validates data; it never changes allocation rules or sends an order.
- **Decision engine:** pure, deterministic transformation of a valid snapshot, the strategy config, and derived monthly state. It has no network, Telegram, or exchange-order dependency.
- **Ledger/reconciliation:** all manual/project interfaces use `LedgerGateway` for `record_execution`, `correct_execution`, `cancel_execution`, and `get_portfolio_state`. Chat 03 is the preferred manual intake surface, but explicit user-confirmed executions/corrections from any BTC DCA project conversation are valid reconciliation evidence. Once reconciled, the GitHub execution ledger is authoritative for runtime monthly spend and portfolio state; chat memory and summaries are never authoritative.
- **Notifier:** transports an already-created event to Telegram and records delivery status. It must not be treated as execution confirmation.
- **Orchestrator:** assigns run/correlation IDs, invokes components, persists artifacts, and applies failure policy.

## 4. V1 decision invariants

The canonical config preserves the approved V1 rules exactly: $10 daily core/minimum, $500 hard calendar-month cap, 168-hour rolling-high drawdown bands, Fear & Greed multipliers, nearest-dollar rounding, and no carry-forward. `nearest_whole_usd` canonically uses decimal `ROUND_HALF_UP`, so an exact positive `.50` tie rounds to the next whole dollar. To remove ambiguous prose boundaries, exact -5%, -10%, -15%, and -25% values resolve to the less-severe band (`>=` its lower limit); values below -25% use $100. The cap is a limit, not a target. A remaining budget below $10 produces $0 and a cap-reached outcome. If a whole-dollar calculated allocation exceeds a remaining budget of at least $10, the final purchase is the remaining budget rounded down to the largest whole-dollar amount that cannot exceed it. Thus fractional confirmed spend can never cause the $500 cap to be exceeded, and `final_purchase_usd` remains an integer.

A current price above its stated rolling 7-day high is contradictory input. It is rejected rather than clamped or converted into a positive drawdown.

The normal daily V1 calculation is recommendation-only. An operator executes any purchase separately and reconciles it after confirmation. Guarded Phase 5 canary/live-order code is outside the normal daily path and remains blocked by checked-in production defaults unless a separate, explicit authorization process changes those controls.

## 5. Failure policy

- Missing, stale, contradictory, incomplete, or invalid approved-Spot market data fails closed; no allocation is fabricated.
- Market-source order is fixed: Bybit Spot -> Binance Spot -> KuCoin Spot. Fallback occurs only after the complete preceding venue fails validation. A price from one venue may never be combined with a 168h high from another.
- Perpetuals, futures, options, mark/index prices, leveraged products, TradingView runtime fallback, unapproved exchanges, and mixed-source calculations are prohibited for V1 execution inputs.
- Invalid Fear & Greed data also yields `data_unavailable`; it is a required V1 input for a final recommendation.
- A schema validation, ledger parse, or monthly-cap derivation failure stops the run before notification.
- Notification delivery failure does not change a decision or create an execution; it is recorded as failed and can be retried idempotently.
- A duplicate `execution_id` or conflicting reconciliation evidence is quarantined for manual review, never auto-merged.

## 6. Security and operational boundaries

API credentials, Telegram bot tokens, and exchange credentials are secrets: use GitHub Actions secrets or the deployment environment only; never commit them or include them in snapshots/logs. GitHub Actions should have least-privilege permissions. Public market-data collection is read-only. Private Bybit/canary components must remain behind the checked-in execution gates and separate explicit authorization. Human-confirmed execution evidence remains the boundary for normal manual ledger reconciliation.

## 7. Versioning and compatibility

Strategy and contract versions are independent semantic versions. A V1 rule change is not an in-place edit: create a proposed version, document migration impact, obtain explicit approval, and retain old artifacts with their original version. New optional schema fields can be minor-compatible; removals, changed meanings, or stricter required fields require a major version. IDs (`snapshot_id`, `decision_id`, `execution_id`, `event_id`) are immutable and correlation IDs connect a run’s artifacts.

`PortfolioState` 1.1.0 is an additive contract evolution over 1.0.0. It retains every 1.0.0 field and adds the derived totals, counts, cap, nominal BTC, weighted reference price, and explicit quantity disclaimer already produced by the offline portfolio derivation. The original 1.0.0 schema remains archived and boundary validation dispatches by the declared version; producers must never label expanded output as 1.0.0. `PortfolioSummary` remains a source-compatible alias for the `PortfolioState` model. `as_of_utc` is the newest confirmed execution timestamp in the source ledger; for an empty ledger it is deterministically the first instant of the requested calendar month.

## 8. Operating roles

| System/channel | Responsibility | Authority boundary |
|---|---|---|
| Chat 01 | strategy mandate and approved operating rules | approves changes to the strategy mandate; not a runtime data store |
| Chat 02 | daily market/sentiment research and calculation input review | research only; cannot override config |
| Chat 03 | preferred manual portfolio/budget entry and reconciliation surface | uses the same Ledger Gateway as every other project interface; no independent portfolio state |
| Chat 04 | architecture, backtest, and controlled evolution | proposes/validates architecture; cannot silently change live V1 |
| GitHub | versioned code/config/contracts/automation and audit history | no secret or manual-execution substitute |
| Telegram | delivery channel for notification events | notification is not order confirmation |
| Bybit | primary BTCUSDT Spot market-data source and separately guarded execution venue | daily recommendation path uses Spot market data; live submission remains disabled by default |
| Binance | first approved BTCUSDT Spot market-data fallback | market data only; never an execution venue for this strategy |
| KuCoin | second approved BTC-USDT Spot market-data fallback | market data only; never an execution venue for this strategy |

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
