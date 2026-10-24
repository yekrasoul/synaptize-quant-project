# Test Coverage Migration

PR #75 reduced the suite from 607 to 598 by replacing implementation-coupled
watchdog JavaScript tests with canonical Python scheduler-policy tests. This
audit removes nine Branch Guardian tests because the Guardian implementation,
workflow and schema are removed as redundant governance.

| Removed coverage | Behavior | Replacement / disposition |
|---|---|---|
| Branch diff regex classification | Classify broad text as safety-sensitive | Removed: Ruleset and required Canonical CI are the governance control; regex was not an execution safety invariant. |
| Guardian protected file list | Force manual branch handling | Removed: stale path list duplicated protected-main policy. |
| Guardian trusted workflow/auto-merge assertions | PR/merge orchestration | Removed: no auto-merge agent remains; PR + Ruleset are authoritative. |
| Hard-coded ledger digest assertion | Detect ledger change | Replaced by canonical validation and `git diff` in local validation; ledger integrity behavior remains covered by ledger tests. |

Preserved test coverage includes DCA allocation/cap, market data, schemas,
artifacts, ledger integrity, Bybit parsing/transports, credential/account
checks, idempotency, pre-network evidence, ambiguous submission, reconciliation,
monitoring freshness and monitor/evidence semantics. No test invokes a real
order or requires production credentials.
