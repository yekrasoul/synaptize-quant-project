# BTC Adaptive DCA — Project Chat Contract

This contract defines mandatory behavior for **every ChatGPT conversation/interface in the BTC ADAPTIVE DCA project**.

## Canonical source of truth

The only authoritative execution-history source is:

`btc_dca_bridge/ledger/executions.jsonl`

accessed through the repository's `LedgerGateway`.

Chat memory, previous chat summaries, scheduled-task prompts, assistant memory, and copied portfolio totals are **not** authoritative execution state.

## Mandatory READ rule

Before answering any question involving:
- total BTC DCA spend;
- current-month spend;
- remaining monthly budget;
- execution count;
- last confirmed purchase;
- portfolio state;
- weighted reference acquisition price;
- whether a purchase has already been recorded;

the interface MUST freshly read the canonical GitHub ledger and derive state through the active ledger projection / `LedgerGateway.get_portfolio_state`.

If the ledger cannot be read, parsed, validated, or projected safely, state that portfolio state is unavailable. Do not fall back to chat memory.

## Mandatory WRITE rule

When the user explicitly confirms a real BTC purchase in any project conversation, reconcile it into the canonical ledger.

Examples of confirmation intent:
- "I bought $25 BTC at 82,000 today."
- "خریدم ۲۵ دلار روی ۸۲۰۰۰"
- "ثبت کن: $30 BTC @ 81,500"

Use `record_execution`.

A recommendation, plan, scheduled output, intended purchase, or hypothetical purchase MUST NOT be written as an execution.

## Mandatory CORRECTION rule

When the user corrects a previously recorded execution, use `correct_execution`.

Examples:
- wrong USD amount;
- wrong execution price;
- wrong execution timestamp;
- a later message explicitly replacing a prior recorded purchase.

Corrections are append-only reconciliation events. Never edit or silently delete historical ledger rows.

## Mandatory CANCELLATION rule

When the user explicitly says a recorded purchase did not happen, use `cancel_execution`.

Cancellation is append-only and must supersede the active execution. Never silently delete history.

## Cross-interface rule

Chat 03 — Portfolio & Budget Tracker is the **preferred manual-entry interface**, but has no higher data authority than any other project conversation.

A confirmed execution entered in Chat 03, this chat, or any other BTC DCA project interface MUST converge into the same canonical ledger.

If a chat summary conflicts with the ledger, the ledger wins until a valid correction/cancellation is explicitly reconciled.

## Idempotency and conflict handling

The same economic execution reported through multiple interfaces must not be double-counted.

For a repeated execution:
- if economic identity matches, treat it as idempotent;
- if the same identity conflicts on amount, price, quantity, or timestamp, stop and surface the conflict;
- do not auto-merge conflicting evidence.

## Post-write rule

After every successful record, correction, or cancellation:
1. re-read the canonical ledger;
2. derive the updated PortfolioState;
3. report the newly derived state rather than remembered totals.

## Daily DCA execution rule

The daily BTC Adaptive DCA V1 calculation MUST derive current-calendar-month spend from the canonical ledger at runtime.

No hard-coded monthly-spend checkpoint may override a readable, valid ledger.

If ledger validation fails, monthly state is unavailable and V1 must fail closed according to strategy policy.

## Strategy separation

This contract changes only execution-state synchronization. It does **not** modify BTC Adaptive DCA V1 allocation rules, market-data rules, monthly cap, or sentiment logic.

Any strategy-rule change remains a separate PROPOSED V2 CHANGE and requires explicit approval.
