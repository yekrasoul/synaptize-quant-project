# Monitoring and Platform Architecture

## Monitoring ownership and semantics

`config/runtime.yaml` is the canonical policy owner for production-shadow
cadence, timeout, and GitHub-delivery lateness tolerance.  The shadow workflow
duplicates only the cron and timeout GitHub must evaluate before Python starts;
tests compare those literals with validated config.  `monitoring.schedule` owns
all scheduler classification.  Workflows collect bounded evidence and invoke
that policy; they do not infer cron slots from their own delayed start time.

GitHub schedule is best-effort: delayed delivery and dropped runs are documented
possibilities.  Therefore `created_at` is evidence freshness, not slot identity.
The scheduler SLO is cadence + configured delivery tolerance + job timeout.
The current 6h + 4h + 30m value is intentionally explicit and reviewable.

- `MONITOR_EXECUTION_STATUS` says whether collection/evaluation completed.
- `SYSTEM_HEALTH_STATUS` says what the monitor observed.
- `SCHEDULER_HEALTH_STATUS` is the schedule-policy result.

An observed ALERT/DEGRADED/UNKNOWN sends an operator notification but does not
make a successfully completed monitor process look broken.  Evidence collection
failure remains a monitor failure.  Unknown evidence is never treated as healthy.

## Current component classification and migration action

| Area | Modules | Boundary | Action |
|---|---|---|---|
| CORE | `models`, `execution`, `ledger`, `ledger_gateway`, `ledger_sync`, `portfolio`, `operations`, `schemas` | execution state, immutable/canonical ledger, reconciliation | KEEP, then SPLIT canonical order/portfolio events from V1 fields |
| STRATEGY | `engine`, `shadow`, `production`, `sentiment/*` | Adaptive DCA V1 decision and shadow orchestration | WRAP; move strategy policy behind a strategy interface |
| VENUE | `private_bybit`, `bybit_submission`, `bybit_spot_transport`, `market_data/bybit`, `availability`, `quote_limits` | Bybit HTTP/private reads/Spot contract | WRAP; move to a Bybit adapter package without changing wire/retry behavior |
| INFRASTRUCTURE | `artifacts/*`, `config`, `paths`, `notifications/*`, `monitoring/*`, `health_monitor`, `project_manifest` | persistence, config, notification, operational evidence | KEEP; SPLIT monitoring and schema/config ownership as adapters grow |
| INTERFACE | `cli`, `__main__` | command parsing and operator boundary | SPLIT: keep commands thin and inject services |
| SAFETY/OPERATIONS | `readiness*`, `final_readiness`, `canary_readiness`, `live_readiness`, `live_activation`, `real_money_authorization`, `production_enablement`, `production_evidence`, `production_status`, `runtime_execution`, `live_order`, `canary`, `blocked_production` | authorization, activation, one-shot submission, evidence, reconciliation gates | KEEP safety semantics; WRAP into explicit authorization and execution services |
| LEGACY CANDIDATES | direct filesystem paths in `paths`, broad CLI imports, duplicate readiness layers | V1 process coupling rather than reusable domain contracts | DEPRECATE only after adapter/service extraction; do not remove now |

## Coupling audit findings

`cli.py` is the primary composition root but imports strategy, venue, safety,
ledger, and artifact concerns directly; it should become a thin interface.
`execution.py` combines domain validation with config and ledger reads, while
`private_bybit.py` and `bybit_submission.py` carry venue-specific transport and
post-ack reconciliation.  Preserve the one-shot/ambiguous-no-retry path while
moving their interfaces behind a canonical OMS/EMS boundary.  `ledger_gateway`
and `ledger_sync` already provide the strongest seam for a future accounting
ledger; retain CAS writes and reconciliation ownership there.  `production*`
and readiness modules are operational policy, not strategy logic; consolidate
their duplicated gates only under characterization tests.

Filesystem artifacts currently carry both operational evidence and domain state.
Keep SHA bindings and immutable storage, but introduce canonical event schemas
before adding venues.  Event time must distinguish market observation time,
decision time, scheduled intent time, GitHub delivery time, and execution time.

## Target-compatible migration sequence

1. Define canonical assets, markets, order intent/state, fills, risk decisions,
   and accounting events beside the existing schemas.
2. Wrap Adaptive DCA V1 as Strategy #1 and Bybit Spot as Venue Adapter #1;
   leave current economics and transport untouched.
3. Extract OMS/EMS interfaces around the existing authorization, idempotency,
   one-shot submission, and reconciliation contracts.
4. Add research adapters (Parquet/Arrow/DuckDB, VectorBT) and simulation before
   CCXT/Binance/Hyperliquid/DEX execution adapters.
5. Evaluate NautilusTrader and portfolio libraries against these canonical event
   and risk interfaces, never as a replacement for existing safety gates.

No multi-venue implementation is part of this change.  In particular, current
Bybit Spot-only, no-leverage, no-borrowing, monthly-cap, approval, reconciliation,
and `RETRY_FORBIDDEN` invariants remain owners of production behavior.
