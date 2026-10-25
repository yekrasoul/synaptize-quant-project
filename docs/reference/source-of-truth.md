# Source of Truth

Use the owning artifact for the question at hand. This index points to existing authority rather than copying its content.

| Concern | Canonical authority |
|---|---|
| Active strategy identity and paths | [`project_manifest.yaml`](../../btc_dca_bridge/config/project_manifest.yaml) |
| V1 rules | [`strategy_v1.yaml`](../../btc_dca_bridge/config/strategy_v1.yaml) and [project specification](../../btc_dca_bridge/docs/PROJECT_SPEC.md) |
| V1 market source | [`market_data.yaml`](../../btc_dca_bridge/config/market_data.yaml); [Bybit Spot adapter contract](../../btc_dca_bridge/docs/BYBIT_SPOT_ADAPTER.md) |
| Runtime/execution policy | [`runtime.yaml`](../../btc_dca_bridge/config/runtime.yaml), [`execution.yaml`](../../btc_dca_bridge/config/execution.yaml), and the [V2 activation checklist](../../btc_dca_bridge/config/v2_activation_checklist.yaml) |
| Data contracts | [`schemas/`](../../btc_dca_bridge/schemas/) |
| Historical execution events and active spend projection | [`ledger/executions.jsonl`](../../btc_dca_bridge/ledger/executions.jsonl), [Ledger Gateway](../../btc_dca_bridge/docs/LEDGER_GATEWAY.md), and [ledger reconciliation](../../btc_dca_bridge/docs/LEDGER_RECONCILIATION.md) |
| V2 architecture and routing status | [Architecture V2](../../ARCHITECTURE_V2.md), [Execution V2](../../EXECUTION_V2.md), [Bybit/CCXT cutover](../../BYBIT_CCXT_CUTOVER.md), and [single-writer audit](../../SINGLE_WRITER_MIGRATION_AUDIT.md) |
| Current readiness claim | Checklist and current machine status/evidence; historical status prose is not a substitute for current gates. |
| Human onboarding and definitions | [Start Here](../START_HERE.md), [baseline](../PROJECT_BASELINE.md), and [glossary](glossary.md) |

When sources disagree, prefer the machine-enforced config/contract for its domain and record unresolved contradictions rather than inferring readiness. Venue fill evidence plus reconciliation is authoritative for executed purchases; notifications, acknowledgements, chat summaries, and strategy decisions are not.
