# Architecture Inventory

This inventory is based on source imports, workflow call paths, schemas, tests,
and CLI dispatch—not names alone. Classification is current-state ownership;
the action is the intended migration decision.

| Component group | Classification | Action | Evidence / owner |
|---|---|---|---|
| `models`, `ledger`, `ledger_gateway`, `ledger_sync`, `portfolio` | CORE | KEEP/SPLIT | Canonical execution state, CAS ledger writes and portfolio projection. |
| `execution` | CORE | SPLIT | Holds order intent/state semantics but imports config, schema and ledger concrete dependencies. |
| `engine`, `shadow`, `production`, `strategy_v1.yaml` | STRATEGY | WRAP | Current Adaptive DCA V1 decision and read-only operational path. |
| `private_bybit`, `bybit_spot_transport`, `bybit_submission`, `live_order` | VENUE + CORE mixed | MERGE/SPLIT | Three signing/transports and two reconciliation/request representations. Characterization required before change. |
| `market_data/bybit`, `market_data/http`, `market_data/provider` | VENUE/INFRASTRUCTURE | MOVE | Read-only Bybit market adapter and generic HTTP policy. |
| `artifacts`, `schemas`, `config`, `paths`, `notifications`, `monitoring` | INFRASTRUCTURE | KEEP/SPLIT | Durable evidence, schema/config and observability ownership. |
| `readiness*`, `final_readiness`, `canary_readiness`, `production_*`, `operations`, `blocked_production` | APPLICATION/SAFETY | MERGE | Repeated gate/check projections need one evaluator per invariant. |
| `cli`, `__main__` | INTERFACE | SPLIT | CLI composes every domain concern and is not a thin interface. |
| `branch_guardian`, Guardian workflows, schema, CLI command, docs | LEGACY | REMOVE | Duplicates active Ruleset + required Canonical CI and has no production-execution safety role. |
| Phase-specific docs | LEGACY | DEPRECATE/MOVE | Historical design records; must not coexist as competing operator instructions. |

## Removal/merge candidates

| Candidate | Callers | Independent invariant? | Duplicate / replacement | Risk and compatibility |
|---|---|---|---|---|
| Branch Guardian stack | Guardian workflows, CLI, one test module, schema registry, docs, post-validation script | No: main protection is Ruleset + required Canonical CI | Replace with Ruleset and `ci.yml` | No historical execution artifact dependency; remove CLI/schema/docs references together. |
| Watchdog workflow | schedule only | No independent health owner after PR #75 | System Health Monitor + `monitoring.schedule` | Consolidate only after workflow-level notification/freshness characterization. |
| Bybit signing/transports | canary/live-order/submission/private read paths | Partly: each carries safety behavior | One venue adapter plus core ports | High: preserve one-shot POST, durable pre-network journal, reconciliation and ambiguous retry prohibition. |
| Readiness layers | CLI/workflows/status and execution chain | Individually mixed | Canonical invariant evaluators | High: characterize each unique gate before consolidation. |

## Constraint taxonomy

- Required safety: ledger CAS/integrity; read-before-write reconciliation;
  idempotency; durable pre-network journal; account/credential scope; no blind
  retry after ambiguity; explicit authorization; current no-leverage/no-borrowing.
- Strategy requirements: current DCA allocation and $500 cap; `btc_adaptive_dca_v1`.
- Venue requirements: Bybit Spot BTCUSDT mapping, Bybit signing and response parsing.
- Accidental complexity: Guardian branch regexes, stale `active_strategy.json`,
  hard-coded guardian ledger digest, branch-name policy, auto-merge orchestration.
