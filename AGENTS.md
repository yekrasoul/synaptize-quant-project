# Agent Working Agreement

## Canonical authority

- GitHub `main` is the canonical repository state. Fetch it before resuming work; do not assume a remembered SHA is current.
- Read [`docs/START_HERE.md`](docs/START_HERE.md), then follow its links to the owning configuration, contracts, and documentation.
- Configuration and active contracts govern their domains. Historical reports are evidence of past state, not current policy.
- Distinguish **CURRENT CANONICAL** behavior from **TARGET** architecture. A target design or passing unit test does not establish production reachability or activation readiness.

## Safety and change scope

- Preserve the approved LIVE V1 strategy economics. Change them only under an explicit, separately reviewed strategy-change task.
- Preserve fail-closed execution controls. Explicit task authorization is necessary but never sufficient for real-money execution or policy changes: all canonical readiness gates, fresh Risk checks, and intended-account restrictions still apply. Never activate execution, disable the kill switch, or submit/cancel orders to complete documentation or ordinary engineering tasks.
- Follow the single [Codebase-to-PR Workflow](docs/ENGINEERING_WORKFLOW.md): verify a fresh baseline, identify a real invariant, make one focused change, validate, and update one PR. Do not create PRs or GitHub Actions without demonstrated need.
- Prefer focused work and the minimum changes needed to complete the task. Inspect the current worktree and relevant source before editing; do not overwrite unrelated work.
- Verify claims against canonical source and report unresolved conflicts instead of guessing.

## Code intelligence

- Graphify is optional. Use it only when its CLI and index are available and current for the checked-out commit.
- If Graphify is unavailable or stale, use targeted source inspection (`rg`, tests, and direct file reads); do not install replacements or reconfigure indexing as part of ordinary work.
- Graph-derived paths and impact analysis are navigation aids, never test, runtime, or verification evidence.

## Interrupted work

Use [`docs/continuity/RESUME_PROTOCOL.md`](docs/continuity/RESUME_PROTOCOL.md) to resume an interrupted task. Keep task-specific progress in the canonical PR/Issue and its checkpoint; do not depend on chat history or create generated checkpoint files.
