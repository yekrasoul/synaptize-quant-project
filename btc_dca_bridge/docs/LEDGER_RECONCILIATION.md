# Execution ledger and reconciliation

`../ledger/executions.jsonl` is the canonical, append-only execution ledger. Each line is one `Execution` object conforming to `../schemas/execution.schema.json`.

The current 12 reconciled records total **$220 in September 2026** and **$95 in October 2026 through 8 October**. `btc_quantity` is intentionally `null`: the available historical evidence provides a BTC reference price but not a verified filled BTC quantity. It must not be inferred from USD ÷ reference price.

Only confirmed executed purchases supported by project conversation evidence enter the ledger. Recommendations, schedules, and superseded intended purchases do not. A later correction is represented by its confirmed record and a reconciliation note; do not add the replaced intended entry.

The partially visible Chat 03 reference to 6 September 2026 remains excluded because its replacement amount and price are not recoverable with confidence. This is an explicit data gap, not a zero purchase.

Derived portfolio/monthly state must be calculated from the ledger at read time. It must not be copied into a README, prompt, or source file.

On 8 October 2026, a user-confirmed **$35** BTC purchase at reference price **$81,865** was reconciled as one actual execution. The stated intent was **$25 for 8 October plus $10 catch-up for 7 October**. The ledger preserves the actual execution date (8 October) and does not fabricate a separate 7 October fill. No 6 October execution is recorded.
