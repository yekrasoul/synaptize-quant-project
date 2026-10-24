# Migration Matrix

## Dependency direction target

```text
interfaces/cli -> application services -> core ports/models
strategy/adaptive_dca_v1 -> core contracts
venues/bybit -> core ports
infrastructure/{artifacts,ledger,config,schemas,monitoring} -> core ports
```

Core must not import Bybit, CLI, concrete paths, or workflow tooling. Strategy
must not import submission transport. Venue must not import DCA economics.

## Immediate safe changes

| Change | Replacement owner | Compatibility |
|---|---|---|
| Remove branch guardians | GitHub Ruleset + Canonical CI | No persisted production state relies on Guardian results. |
| Remove Guardian CLI/schema/tests/docs/script check | No replacement API; use PR/Rulset enforcement | Branch-only developer interface removed. |
| Retain monitoring policy in Python | `monitoring.schedule` | Workflow remains orchestration only. |
| Pin reproducible workflow tooling | workflow `setup-uv` | No domain behavior changed. |

## Deferred, characterization-first changes

| Area | Required characterization before modification |
|---|---|
| Bybit transport/reconciler merge | POST count, signing bytes, persisted journal, response mapping, timeout/ambiguous reconciliation and ledger-write ordering. |
| Execution ceremony simplification | Threat matrix proving each removed artifact has no independent authorization/environment/capability role. |
| Readiness merge | One table mapping every invariant to current evaluator and the selected canonical owner. |
| Filesystem repository boundary | Historical artifact read compatibility fixtures. |
| CLI split | Stable command/exit-code compatibility tests. |

## Artifact and schema ownership

- Domain events: `decision`, `execution`, `order_intent`, `order_submission*`,
  `submission_reconciliation`, `reconciliation_result`.
- Domain snapshots: `market_snapshot`, `sentiment_snapshot`, `portfolio_state*`,
  `shadow_run`, `production_status*`.
- Authorization/evidence: `canary_manifest`, `live_execution_approval`,
  `real_money_authorization`, `execution_activation`, `production_enablement*`,
  `production_evidence*`.
- Infrastructure/legacy: `notification_event`, `safety_validation`,
  `branch_guardian_result` (remove with Guardian).

Historical schema versions remain readable; no historical artifact is rewritten.
