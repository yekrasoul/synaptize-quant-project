# BTC DCA Branch Guardian v1

Branch Guardian validates `feat/**`, `fix/**`, and `refactor/**` pushes on
GitHub-hosted infrastructure. The validation job has read-only repository and
Actions permissions, receives no Bybit credentials, and runs the canonical
compile, unit, validation, and blocked-runtime checks.

It classifies changes conservatively. Protected execution configuration,
active strategy, Production Shadow, and Guardian trust-surface changes are
`SAFETY_SENSITIVE`; ledger changes are `FAIL`; safety-semantic matches are
also blocked from automatic merge. Semantic matching is applied only to
actual added/removed diff content, while protected-file and ledger checks use
the structured changed-file set. Runtime invariants are independently loaded
from the repository.

The branch workflow is strictly unprivileged: it checks out the branch,
runs validation, produces a result artifact for diagnostics, and has only
`contents: read` and `actions: read`. It cannot create or merge pull requests.

The separate `branch-guardian-trusted.yml` workflow is loaded from the default
branch and is triggered by completion of the validation workflow. It does not
trust the branch result artifact. Instead, it checks out `main`, fetches the
exact source commit, recomputes classification with trusted code, verifies
the branch relationship and runtime invariants, and requires the exact
successful `Canonical validation` check on the exact PR head SHA before any
PR operation. It creates at most one PR and requests
`gh pr merge --auto --merge` only for `SAFE_ENGINEERING` after bootstrap is
explicitly enabled.

The first pilot has a bootstrap guard. Until repository variable
`BRANCH_GUARDIAN_BOOTSTRAPPED=true` is deliberately set after manual review,
the result remains manual-review-only. This is not a safety bypass and cannot
enable execution.

Required checks are `Canonical validation`, `Branch Guardian / Validation`,
and `Branch Guardian / Safety classification`. No required check is bypassed.
Guardian workflow, classifier, and other trust-surface changes always require
manual review.

Trusted auto-merge pilot verified.
