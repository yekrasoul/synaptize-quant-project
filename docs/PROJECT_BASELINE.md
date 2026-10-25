# Project Baseline

This baseline describes the checked-in repository state. Configuration and active canonical documents take precedence over old phase reports.

## Product and market data

BTC Adaptive DCA V1 is recommendation-first. Bybit BTCUSDT Spot is the sole V1 market-data source; price and rolling 168-hour high must be from that source, and missing or invalid Bybit data fails closed. Alternative.me is the configured sentiment source. See the [project specification](../btc_dca_bridge/docs/PROJECT_SPEC.md), [market data config](../btc_dca_bridge/config/market_data.yaml), and [adapter guide](../btc_dca_bridge/docs/BYBIT_SPOT_ADAPTER.md).

## Execution architecture

V2 is the target execution architecture: StrategyDecision → OrderIntent → Risk → OMS → EMS/VenueAdapter → reconciliation → accounting. The durable OMS owns mutable order lifecycle state and its execution record is an audit projection. The V2 composition and single-writer route are not permission to activate execution; checked-in [V2 runtime policy](../btc_dca_bridge/config/runtime_execution_policy.yaml) and [activation checklist](../btc_dca_bridge/config/v2_activation_checklist.yaml) remain authoritative. Read [Architecture V2](../ARCHITECTURE_V2.md), [Execution V2](../EXECUTION_V2.md), and [cutover evidence](../BYBIT_CCXT_CUTOVER.md).

Historical Phase 5 components remain for compatibility, audit, and read-only reconciliation; `canary-execute` is retired and fail-closed, not a production submit path. See the [single-writer migration audit](../SINGLE_WRITER_MIGRATION_AUDIT.md) and [cutover record](../BYBIT_CCXT_CUTOVER.md) for per-entrypoint reachability.

## Accounting and reconciliation

Venue fill evidence establishes execution truth; reconciliation must confirm it before accounting. The existing JSONL ledger is the canonical historical execution source and V1 monthly-spend authority. V2 accounting accepts confirmed reconciled entries and the unified spend projection includes confirmed V2 records. Chat summaries, notifications, acknowledgements, and decision artifacts are not execution records. See [ledger reconciliation](../btc_dca_bridge/docs/LEDGER_RECONCILIATION.md) and [accounting architecture](../ACCOUNTING_ARCHITECTURE.md).

## Known documentation conflicts

- Older phase records contain historical status snapshots. The [V2 cutover record](../BYBIT_CCXT_CUTOVER.md) identifies the pre-cutover matrix, and the current [activation checklist](../btc_dca_bridge/config/v2_activation_checklist.yaml) is the active machine-readable gate. Activation is blocked while account/environment identity and credential scope/withdrawal exclusion are unverified and Production Shadow V2 pre-network evidence is partially verified.
