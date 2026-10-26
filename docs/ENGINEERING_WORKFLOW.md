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

## Multi-agent protocol for one approved task

Use this protocol only for an explicitly authorized, bounded task. The coordinator first refreshes `origin/main` and checks source, relevant PRs, tests, and Canonical CI evidence; agents must not rely on remembered state. If the evidence shows no real gap or duplicate work, record **NO_CHANGE** in the existing Issue/PR when one exists and open no PR.

The coordinator defines the invariant, affected files, acceptance evidence, dependencies, and stop conditions. Exclude production configuration, secrets, live trading, activation, and account-state mutations. Use read-only discovery and safety roles, exactly one implementation writer, and an independent reviewer. The writer makes the smallest coherent diff and does not refactor outside scope.

Validate with focused tests and the canonical checks, then have an independent, read-only reviewer who did not author the final diff inspect correctness and safety and record a verdict on the final PR HEAD. For a reproducible failure, make at most two bounded fixes; never weaken tests to turn a gate green. After every writer repair, rerun the independent review against the new final PR HEAD. Deliver at most one PR for a real change, verify Canonical CI on its exact head SHA, and leave it ready for review. Do not auto-merge unless an independently preapproved repository policy permits that category and GitHub protections authorize it. Resume from the existing Issue/PR checkpoint only after refreshing and comparing `origin/main`; do not create a separate progress database.

The existing PR or Issue checkpoint must record the baseline SHA; the one writer's identity/session; the reviewer identity/session and verdict; reviewed and final PR-head SHAs; focused tests; the Canonical CI run for that same SHA; and the exact next action or **NO_CHANGE** reason.

When using the locally supported Codex CLI, this is a coordinator-session pattern, not a scheduler or delegation claim:

```sh
codex exec --worktree 'Coordinate one approved bounded task: define roles, evidence, stop conditions, and checkpoint requirements. Stop with NO_CHANGE if no gap is evidenced.'
codex review --base origin/main 'Independently review the completed task for its stated invariant, safety boundaries, and test evidence.'
```

`--worktree` provides isolation for a coordinator session and `review` can run an independent review. Separate discovery, safety, writer, and reviewer sessions require the invoking environment to provide that capability; the commands above neither launch nor enforce those role handoffs. Branch/PR ownership and the role handoffs remain explicit coordinator responsibilities.

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
