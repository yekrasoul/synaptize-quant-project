# Ledger Gateway

The Ledger Gateway is the canonical read/write boundary for BTC Adaptive DCA execution history.

## Goal

Every interface must converge on the repository-declared canonical portfolio state:

```text
ChatGPT / connector ─┐
Codex / local CLI ───┼─> versioned LedgerGateway ─> manifest ledger ─> active projection ─> PortfolioState
Other adapter ───────┘
```

No chat summary, remembered total, or scheduled-task prompt is a source of truth for executed spend.

## Canonical operations

### Manual/local CLI

The `btc-dca` entrypoint is a thin wrapper over `LedgerGateway`; these commands reconcile accounting only and never contact an exchange or submit an order:

```bash
btc-dca ledger-state --month 2026-10
btc-dca ledger-record --executed-at 2026-10-09T12:30:00Z --usd 25 --reference-price 82000 --source "Codex CLI" --note "user-confirmed execution"
btc-dca ledger-correct --target-execution-id execution_existing --executed-at 2026-10-09T12:30:00Z --usd 24 --reference-price 82000 --source "Codex CLI" --note "user-confirmed correction"
btc-dca ledger-cancel --target-execution-id execution_existing --cancelled-at 2026-10-09T12:35:00Z --source "Codex CLI" --note "user-confirmed cancellation"
```

Correction and cancellation require the exact active target ID. A same-day same-economics purchase that could be a duplicate is blocked unless the caller supplies both an explicit distinct-execution intent and a distinct execution ID. Every successful operation re-reads and returns the derived active projection/PortfolioState.

### record_execution

Use when the user explicitly confirms that a BTC purchase actually executed.

- Appends a reconciled execution to `ledger/executions.jsonl`.
- Generates a deterministic manual execution ID from execution time, USD amount, reference price, and optional BTC quantity.
- Repeating the same deterministic execution from another interface is idempotent. A same-day economic match with timestamp differences is rejected as ambiguous rather than merged.
- Intake provenance is bounded (`chat_01`…`chat_04`, `project_chat`, `codex_cli`, `github_connector`, `automation`); provenance never changes economic identity or portfolio authority.
- Recommendations and intended purchases are not executions.

### correct_execution

Use when the user corrects an already-recorded execution.

- Never edits or deletes the original row.
- Appends an immutable correction event with `status=reconciled` and `supersedes_execution_id`.
- The active projection removes the superseded execution and counts only the correction.
- Audit history remains intact.

### cancel_execution

Use when the user states that a recorded execution did not occur or must be voided.

- Never deletes the original row.
- Appends an immutable event with `status=voided` and `supersedes_execution_id`.
- The active projection excludes the superseded execution from spend and portfolio calculations.

### get_portfolio_state

Use for every portfolio/budget answer.

- Reads the ledger fresh.
- Resolves corrections and voids into the active execution set.
- Loads the canonical V1 monthly cap from strategy config.
- Derives total spend, current-month spend, remaining budget, execution counts, nominal BTC, and weighted reference acquisition price.

## Interface contract

For every BTC DCA project interface:

1. Before reporting portfolio state, read through the Ledger Gateway; never rely on chat memory.
2. When the user explicitly confirms a real execution, call `record_execution`.
3. When the user corrects a previously recorded execution, call `correct_execution`.
4. When the user says a recorded execution did not happen, call `cancel_execution`.
5. After any successful write, re-read `get_portfolio_state` and report that derived state.
6. If the ledger cannot be read, validated, or reconciled, fail closed rather than guessing.
7. Chat 03 remains the preferred manual-entry surface, but it has no higher data authority than another project chat once facts are reconciled into the ledger.

## Versioned synchronization and append-only reconciliation

`VersionedLedgerStore` exposes `read() -> (content, version)` and compare-and-swap. The version is an immutable token: GitHub Contents adapters use the current blob SHA; local adapters use the digest of the exact canonical bytes/current Git state. Other transports must provide equivalent atomic CAS. Business logic never contains GitHub credentials. A CAS conflict causes a bounded fresh read, validation, and replay of the same semantic operation; exhaustion fails without overwrite. All updates must preserve prior event history.

### GitHub Contents adapter contract

For project-chat writes, fetch the manifest and its declared ledger from current `main`; fetch the ledger's current GitHub blob/content SHA and use it as the expected-version token. Parse/validate the complete file, apply the semantic operation to the active projection, validate the new complete history/projection/PortfolioState, then update with that expected SHA. On stale-SHA rejection, fetch latest bytes+SHA and re-apply the same operation; retry no more than 3 times. Never force-overwrite. Re-read canonical state after success before reporting. If the connector cannot enforce expected-SHA semantics, do not write.

Legacy schema rows remain readable. Gateway-originated reconciliations use the bounded provenance model in schema 1.3; corrections and voids remain append-only events using `supersedes_execution_id`.

The active execution projection is deterministic:

- normal reconciled row -> active;
- reconciled row that supersedes an active row -> replace it;
- voided row that supersedes an active row -> remove it;
- unknown or already-inactive supersedes target -> validation failure.

This prevents double counting while preserving the complete audit trail.

`ledger_event_count` counts every immutable JSONL row. `active_execution_count`, spend, budget, and derived execution IDs count only the final active projection.
