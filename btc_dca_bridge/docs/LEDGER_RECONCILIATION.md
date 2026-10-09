# Execution ledger and reconciliation

`../ledger/executions.jsonl` is the canonical, append-only execution ledger. Each line is one `Execution` object conforming to `../schemas/execution.schema.json`.

The current 12 reconciled records total **$220 in September 2026** and **$95 in October 2026 through 8 October**. `btc_quantity` is intentionally `null`: the available historical evidence provides a BTC reference price but not a verified filled BTC quantity. It must not be inferred from USD ÷ reference price.

Only confirmed executed purchases supported by explicit user evidence enter the ledger. **Chat 03 — Portfolio & Budget Tracker is the preferred/manual intake surface, but it is not the only admissible intake surface.** A purchase explicitly confirmed by the user in any conversation within the BTC ADAPTIVE DCA project is eligible for reconciliation into the same canonical ledger. The ledger, not any individual chat summary, is the runtime source of truth.

Cross-chat synchronization rule: when the user confirms, corrects, or cancels an execution in any project chat, reconcile that fact into `ledger/executions.jsonl` as soon as repository access is available. Chat 03 summaries must be derived from that ledger and must not overwrite or discard valid executions confirmed elsewhere. If Chat 03 is stale, the ledger wins until Chat 03 is refreshed from it.

Recommendations, schedules, and superseded intended purchases do not enter the ledger. A later correction is represented by its confirmed record and a reconciliation note; do not add the replaced intended entry.

The partially visible Chat 03 reference to 6 September 2026 remains excluded because its replacement amount and price are not recoverable with confidence. This is an explicit data gap, not a zero purchase.

Derived portfolio/monthly state must be calculated from the ledger at read time. It must not be copied into a README, prompt, or source file.

On 8 October 2026, a user-confirmed **$35** BTC purchase at reference price **$81,865** was reconciled as one actual execution. The stated intent was **$25 for 8 October plus $10 catch-up for 7 October**. The ledger preserves the actual execution date (8 October) and does not fabricate a separate 7 October fill. No 6 October execution is recorded.

## Cross-interface gateway

All project interfaces must use `LedgerGateway` as the execution-history boundary. Chat 03 is a preferred manual-entry UI, not a separate data store. Explicit confirmed executions from any BTC DCA project chat are reconciled into the same ledger. Portfolio answers must be freshly derived from the active ledger projection.

Schema 1.2 reconciliation events preserve append-only audit history: a reconciled event with `supersedes_execution_id` replaces one active execution, while a voided event removes one active execution. Unknown, self-referential, or already-inactive supersedes targets fail validation. See `LEDGER_GATEWAY.md`.
