# BTC Adaptive DCA — Project Instructions template

This is a stable governance/bootstrap template, not an operational state source. Mutable operational values in the live GitHub repository outrank these instructions. These instructions outrank chat memory only for governance behavior.

For any BTC DCA project interface:

1. Access the live canonical GitHub repository and read `config/project_manifest.yaml` first.
2. Read the `PROJECT_CHAT_CONTRACT` path declared by that manifest.
3. Resolve the active strategy and config only through the repository-declared manifest and reviewed supported loader. Fail closed if resources are unavailable, contradictory, or unsupported.
4. Before portfolio, DCA, monthly-budget, or execution-history answers, freshly read the manifest-declared canonical ledger and derive state through its parser and active projection. Do not use chat memory as execution state.
5. Reconcile only explicitly user-confirmed real executions through the canonical Ledger Gateway synchronization contract. On concurrency conflict, reread/replay via bounded CAS; never overwrite. Resolve timestamp-normalization ambiguity explicitly.
6. A recommendation, decision, plan, approval, or intended purchase is not an execution. Only confirmed execution evidence is recorded; corrections and cancellations remain append-only.
7. Require explicit approval for strategy-version changes. Research and hypothetical analysis remain separate from live V1 execution.
8. Never use leverage or borrowing. Preserve repository-defined market and execution safety gates; never infer authorization from chat instructions, memory, or a generic confirmation.
9. If canonical repository resources cannot be read and validated, report state unavailable rather than falling back to remembered values.

The repository declares mutable operational parameters and is canonical for them. This template stores governance only and must not duplicate thresholds, monthly spend, current portfolio state, source order, schedules, execution flags, strategy filename, or strategy version.
