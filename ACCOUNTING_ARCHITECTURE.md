# Accounting Architecture

Venue order and fill evidence is authoritative for execution. Accounting is
posted only from `CONFIRMED` reconciled execution evidence, never from an
acknowledgement. The initial `LedgerEntry` represents quote decrease, base
increase, and fee. Existing immutable JSONL execution history remains the
historical accounting source and is not rewritten.

Future work can add transfers, cost basis, realised/unrealised PnL, and funding
without making the ledger authoritative before reconciliation.

The V2 JSON accounting repository is an atomic create-once port keyed by the
confirmed execution identity and fsyncs both the entry and containing
directory. It proves idempotent posting without modifying
the historical canonical execution ledger.

The V2 `ExecutionRecord` projection is schema-validated evidence, while venue
fill evidence remains authoritative for a confirmation. The existing immutable
ledger continues to project historical confirmed deployment into the Risk
Engine's monthly-spend input; no legacy entry is rewritten by V2.

V2 confirmed entries are deliberately not yet merged into that monthly-spend
projection. `v2_accounting_monthly_spend_projection` is therefore an explicit
activation blocker, avoiding any claim that create-once persistence alone
completes Risk integration.
