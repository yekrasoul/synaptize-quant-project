# CLI Migration

`cli.py` is currently a large composition root that imports strategy, ledger,
artifact, readiness, authorization, activation, Bybit, submission and status
implementations directly. The target is parser + application service invocation
+ rendering + exit-code mapping only.

## Immediate deprecation/removal

`branch-guardian-classify` is removed with the redundant Guardian workflow
stack. GitHub Ruleset and the required Canonical CI check replace its branch
classification/auto-merge function.

## Preserved command families

- Read-only: validation, calculation, portfolio, shadow, status, readiness,
  evidence verification, connectivity and reconciliation inspection.
- Explicitly guarded production path: planning, authorization, activation,
  enablement and submission commands retain their current safety semantics
  pending a characterization-first execution-service extraction.

## Future decomposition

1. Move parser definitions and renderers to `interfaces/cli`.
2. Create application services for shadow, status, readiness and execution.
3. Inject ports for artifact/ledger/config/venue access.
4. Keep existing command names as compatibility shims until an explicit
   deprecation window is approved.
