# Ledger Gateway

The Ledger Gateway is the canonical read/write boundary for BTC Adaptive DCA execution history.

## Goal

Every interface must converge on the same portfolio state:

```text
Chat 03 ─┐
Other project chats ─┼─> LedgerGateway ─> ledger/executions.jsonl ─> PortfolioState
Automation / CLI ────┘
```

No chat summary, remembered total, or scheduled-task prompt is a source of truth for executed spend.

## Canonical operations

### record_execution

Use when the user explicitly confirms that a BTC purchase actually executed.

- Appends a reconciled execution to `ledger/executions.jsonl`.
- Generates a deterministic manual execution ID from execution time, USD amount, reference price, and optional BTC quantity.
- Repeating the same economic execution from another project interface is idempotent even when the chat wording differs.
- Recommendations and intended purchases are not executions.

### correct_execution

Use when the user corrects an already-recorded execution.

- Never edits or deletes the original row.
- Appends a schema 1.2 reconciliation event with `status=reconciled` and `supersedes_execution_id`.
- The active projection removes the superseded execution and counts only the correction.
- Audit history remains intact.

### cancel_execution

Use when the user states that a recorded execution did not occur or must be voided.

- Never deletes the original row.
- Appends a schema 1.2 event with `status=voided` and `supersedes_execution_id`.
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

## Append-only reconciliation model

Schema 1.0/1.1 rows represent historical confirmed executions. Schema 1.2 adds append-only correction/void events using `supersedes_execution_id`.

The active execution projection is deterministic:

- normal reconciled row -> active;
- reconciled row that supersedes an active row -> replace it;
- voided row that supersedes an active row -> remove it;
- unknown or already-inactive supersedes target -> validation failure.

This prevents double counting while preserving the complete audit trail.
