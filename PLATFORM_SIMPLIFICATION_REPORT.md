# Platform Simplification Report

## Baseline and current state

- Baseline SHA: `0850570fedd0e0f0e7237171ee96952b664850de`.
- Current branch: `refactor/platform-simplification-audit`.
- Ruleset: verified live; PR + strict Canonical validation + non-fast-forward
  and deletion protection; no bypass actors. Unchanged.

## Measured simplification

| Measure | Before | After |
|---|---:|---:|
| Workflows | 7 | 3 |
| Guardian workflows | 2 | 0 |
| Health workflows | 3 | 1 (`system-health-monitor`) |
| Guardian module/schema/CLI command | 1/1/1 | 0/0/0 |
| Test count | 598 | 585 |

Remaining workflows are Canonical CI, Production Shadow, and System Health
Monitor. System Health owns production-shadow freshness, Bybit contract drift,
CI state, runner/artifact/ledger/notification health and health notifications.

## Removed duplication

- Branch Guardian / trusted Guardian: duplicate validation, branch restrictions,
  stale `active_strategy.json` reference, broad safety regex, hard-coded ledger
  digest, PR creation and auto-merge. GitHub Ruleset and Canonical CI are the
  stronger canonical protection.
- Standalone Production Shadow Watchdog: duplicate scheduler health policy.
- Standalone Bybit Contract Watch: duplicate contract fingerprint, state and
  notification logic.
- Orphaned Guardian schema, CLI command, tests, documentation and post-check
  constraints.

## Retained safety invariants

The implementation leaves DCA V1 economics, monthly cap, Bybit Spot mapping,
no-leverage/no-borrowing, credential/account verification, durable pre-network
evidence, idempotency, reconciliation-before-ledger-write, ambiguous submission
retry prohibition, immutable artifacts, canonical ledger and Ruleset unchanged.
No order, canary execution, authorization, activation, enablement or ledger
mutation was performed.

## Deferred high-risk work

Bybit transport/reconciliation and execution/readiness ceremony consolidation
remain intentionally deferred. They need characterization fixtures for exact
wire bytes, POST cardinality, journal persistence, ambiguous outcomes and
ledger-write ordering before any merge/remove. The detailed ownership,
constraint and migration analysis is in `ARCHITECTURE_INVENTORY.md`,
`MIGRATION_MATRIX.md`, and `CLI_MIGRATION.md`.

## Validation

- `uv sync --frozen`: pass.
- `python -m compileall -q src tests`: pass at baseline; unchanged source
  compilation assumptions are covered by the full suite.
- Focused monitoring/workflow/schema/readiness: 45 passed.
- Full suite: 585 passed in 24.328 seconds.
- `git diff --check`: pass.

Canonical CI and Ruleset were verified for the baseline. CI for this branch
must pass before review/merge.
