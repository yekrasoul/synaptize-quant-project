# Execution ledger and reconciliation

`../ledger/executions.jsonl` is the canonical, append-only execution ledger. Each line is one `Execution` object conforming to `../schemas/execution.schema.json`.

The current 11 reconciled records total **$220 in September 2026** and **$60 in October 2026 through 5 October**. `btc_quantity` is intentionally `null`: the available historical evidence provides a BTC reference price but not a verified filled BTC quantity. It must not be inferred from USD ÷ reference price.

Only confirmed executed purchases from **Chat 03 — Portfolio & Budget Tracker** enter the ledger. Recommendations, schedules, and superseded intended purchases do not. A later correction is represented by its confirmed record and a reconciliation note; do not add the replaced intended entry.

The partially visible Chat 03 reference to 6 September 2026 remains excluded because its replacement amount and price are not recoverable with confidence. This is an explicit data gap, not a zero purchase.

Derived portfolio/monthly state must be calculated from the ledger at read time. It must not be copied into a README, prompt, or source file.
