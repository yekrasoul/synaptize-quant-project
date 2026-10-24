# Platform Simplification Baseline

Captured before structural refactoring on 2026-10-24.

## Repository and governance

- Baseline `origin/main`: `0850570fedd0e0f0e7237171ee96952b664850de`
- Baseline merge: PR #75; Canonical validation passed.
- Audit branch: `refactor/platform-simplification-audit`.
- Initial worktree: clean.
- Live Ruleset `Protect canonical main` is active for `refs/heads/main`.
  It requires pull requests, strict `Canonical validation`, prevents
  non-fast-forward updates and deletion, and has no bypass actors. It is out of
  scope for weakening or removal.

## Repository counts

- Workflows: 7 (`branch-guardian`, `branch-guardian-trusted`,
  `bybit-contract-watch`, `ci`, `production-shadow-watchdog`,
  `production-shadow`, `system-health-monitor`).
- Python source files: 55 (including package modules).
- Test modules: 48.
- JSON schemas: 31.
- Configuration files: 9.
- Documentation files: 31.

## Validation baseline

The following completed before edits:

```text
uv sync --frozen                                  PASS
python -m compileall -q src tests                  PASS
python -m unittest discover -s tests -v            PASS (598 tests, 24.335s)
python -m btc_dca_bridge validate                  PASS
```

Validation confirmed canonical ledger validity (13 execution records), active
strategy `btc_adaptive_dca_v1` / `1.0.0`, and a non-authorized live-execution
state. No production credentials, order, canary execution, authorization,
activation, enablement, or ledger mutation was invoked.

## Public CLI baseline

The CLI has 45 commands. It includes core read-only commands (`validate`,
`portfolio`, `calculate`, `production-shadow`, `production-status`) alongside
historical/ceremonial command families for branch guardians, canary preparation
and execution, authorization, activation, runtime execution, production
enablement, readiness, evidence, and status history. The exact pre-refactor
command list was captured from `python -m btc_dca_bridge --help` in the audit
record.

## Baseline ownership observations

- `strategy_v1.yaml` owns current Adaptive DCA economics; `execution.yaml`,
  market-data config, code constants, schemas, and workflows repeat parts of
  market/execution identity.
- GitHub Ruleset plus required Canonical CI are independent branch protection.
  Branch Guardian workflows and module are candidates for removal only after
  caller and test analysis confirms they add no separate trust boundary.
- Production monitoring was recently centralized into Python schedule policy,
  but four health-related workflows still remain.
- Bybit signing, request mapping, submission persistence, and reconciliation
  occur in more than one module and require a characterization-first audit.

## Baseline inventory locations

- Workflows: `.github/workflows/`
- Runtime configuration: `btc_dca_bridge/config/`
- Schemas: `btc_dca_bridge/schemas/`
- Source: `btc_dca_bridge/src/btc_dca_bridge/`
- Tests: `btc_dca_bridge/tests/`
- Documentation: `btc_dca_bridge/docs/`

The detailed component, migration, constraint, schema, artifact, and test
coverage inventories will be maintained in the companion audit documents.
