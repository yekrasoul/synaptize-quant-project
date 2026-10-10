# BTC DCA Branch Guardian v1

Branch Guardian validates `feat/**`, `fix/**`, and `refactor/**` pushes on
GitHub-hosted infrastructure. The validation job has read-only repository and
Actions permissions, receives no Bybit credentials, and runs the canonical
compile, unit, validation, and blocked-runtime checks.

It classifies changes conservatively. Protected execution configuration,
active strategy, and Production Shadow changes are `SAFETY_SENSITIVE`; ledger
changes are `FAIL`; safety-semantic matches are also blocked from automatic
merge. Classification is based on the structured changed-file set plus the
full diff, with runtime invariants independently loaded from the repository.

The privileged job only consumes the schema-validated result artifact. It
creates one PR if none exists, never duplicates an open branch PR, and requests
`gh pr merge --auto --merge` only for `SAFE_ENGINEERING` when the result says
auto-merge is eligible and the branch is current with `main`.

The first pilot has a bootstrap guard. Until repository variable
`BRANCH_GUARDIAN_BOOTSTRAPPED=true` is deliberately set after manual review,
the result remains manual-review-only. This is not a safety bypass and cannot
enable execution.

Required checks are the canonical `Canonical validation` check and the two
Guardian checks: `Branch Guardian / Validation` and `Branch Guardian / Safety
classification`. No required check is bypassed.
