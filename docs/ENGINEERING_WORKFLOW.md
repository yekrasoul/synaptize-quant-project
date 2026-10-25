# One Change Workflow: Codebase to PR

Use this single lightweight process for human and agent contributions. The existing **Canonical CI** is the only required validation workflow; do not introduce a new Action, approval ceremony, or extra PR for routine work.

## 1. Establish the real baseline

```sh
git fetch origin main
git status --short
git branch --show-current
git rev-parse main origin/main
```

Read [Start Here](START_HERE.md) and [Agent Working Agreement](../AGENTS.md); resolve owning source and tests. If local `main` is clean and has no divergent commits, `git switch main && git merge --ff-only origin/main`. Otherwise stop and inspect; never reset, force-push, or discard unrelated work. Re-check existing PRs before opening work.

## 2. Decide whether code or a PR is warranted

Name the observable problem, affected contract, and **unique invariant** it threatens. Confirm it is not already solved on `main`. Prefer a read-only finding, an existing issue/PR, or no change when there is no necessary implementation. Use an Issue for ongoing multi-step work, not as compulsory ceremony. One cohesive change means one feature branch and at most one PR.

## 3. Scope and implement

Create a branch from verified `main`, e.g. `git switch -c fix/<focused-topic>`. State scope, exclusions, acceptance evidence, and stop conditions. Characterize existing behavior first; then make the smallest diff. Reuse canonical contracts and existing open-source dependencies. Graphify is optional and cannot replace source inspection. Do not add tests that freeze private names, helper ordering, workflow wording, or obsolete CLI ceremony unless they protect a real invariant.

LIVE V1 economics, execution policy, the single active writer, kill switch, and reconciliation/accounting guarantees are not modified during unrelated tasks. Never place secrets or account evidence in PRs/logs.

## 4. Validate before proposing a merge

From `btc_dca_bridge/`, run focused tests for the touched behavior, then the existing canonical checks:

```sh
uv sync --frozen
uv run python -m compileall -q src tests
uv run python -m unittest discover -s tests -v
uv run python -m btc_dca_bridge validate
```

At repository root run `git diff --check` and review the actual diff. Document what was **VERIFIED**, **NOT_VERIFIED**, or not run; never claim operational evidence from mocked tests. If a local environment cannot reproduce CI, report why rather than weakening checks.

## 5. One PR, evidence-based review

Push the branch and open **one PR** to protected `main`. Keep its description short: problem/invariant, precise changes, test results, safety impact, blockers, and linked Issue only if one exists. Canonical CI must pass on the **current PR head SHA** before merge. Review diffs and failure modes independently; do not treat green CI as sufficient for real-money activation. If review finds a defect, update the **same PR** and recheck CI. Do not create follow-up PRs merely for wording or fragmented scope.

## 6. Close and resume

After review and required checks, merge through the repository's existing controls. Re-read `main` and confirm the merge commit, policy, and relevant evidence. Record remaining blockers and the exact next action in the PR/Issue as needed, following [Resume Protocol](continuity/RESUME_PROTOCOL.md). No per-task checkpoint file, duplicate status database, extra automation workflow, or mandatory manual per-order approval.

**Rule:** A PR is an evidence-backed delivery vehicle, not a measure of progress. No real gap means no PR.
