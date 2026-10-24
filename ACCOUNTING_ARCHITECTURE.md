# Accounting Architecture

Venue order and fill evidence is authoritative for execution. Accounting is
posted only from `CONFIRMED` reconciled execution evidence, never from an
acknowledgement. The initial `LedgerEntry` represents quote decrease, base
increase, and fee. Existing immutable JSONL execution history remains the
historical accounting source and is not rewritten.

Future work can add transfers, cost basis, realised/unrealised PnL, and funding
without making the ledger authoritative before reconciliation.
