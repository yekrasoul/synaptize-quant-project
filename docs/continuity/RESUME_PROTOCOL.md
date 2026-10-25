# Resume Protocol

Use this procedure when an engineering task is interrupted. GitHub `main`, the task's GitHub Issue/PR, and committed artifacts are the durable record; chat history is not required.

1. **Verify remote main.** Fetch `origin/main`, record its SHA, and compare it with local `main`. Fast-forward only when local `main` is clean and non-divergent. Never reset, discard commits, or silently rebase shared work.
2. **Inspect branch and worktree.** Record the current branch, `git status`, and local/remote divergence. Preserve unrelated changes. Do not continue edits on an unexpected or dirty worktree until their ownership and safety are clear.
3. **Identify the task.** Locate the associated GitHub Issue and PR from the task reference, branch, or commit. Confirm its objective, review feedback, scope, exclusions, and latest state.
4. **Read the last checkpoint.** Use the task's latest checkpoint in its canonical PR/Issue. If none exists, derive progress from commits, diffs, and CI; do not infer completion from a chat summary.
5. **Reconcile progress with evidence.** Verify each claimed change against actual commits and files. Check test and CI results from the current commit; rerun relevant checks when their evidence is missing, stale, or from another SHA.
6. **Continue at the first unverified step.** Do not repeat completed work unless evidence is stale or the current baseline invalidates it. Update the checkpoint with new evidence and the exact next action.
7. **Stop safely on conflicts.** If execution state, order outcome, reconciliation, or financial/ledger state conflicts or is ambiguous, do not submit, retry, activate, or mutate financial records. Preserve evidence and escalate for authoritative resolution.

Do not treat Graphify output, generated summaries, or claimed progress as verification evidence.

## Checkpoint location

Use the PR/Issue description or a comment with the fields in [`TASK_CHECKPOINT.md`](TASK_CHECKPOINT.md). Do not create per-task generated checkpoint files in the repository.
