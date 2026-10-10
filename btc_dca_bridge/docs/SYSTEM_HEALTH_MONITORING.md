# System health monitoring

The repository uses one read-only monitoring workflow, `.github/workflows/system-health-monitor.yml`, in addition to the production-shadow workflow it observes.

- `17 * * * *` selects `critical` mode. It checks GitHub Actions evidence for the self-hosted runner, production-shadow freshness/state, monitor freshness, canonical CI, retained shadow artifacts, ledger integrity, Telegram capability, and all production safety invariants. It does not call Bybit.
- `43 */6 * * *` selects `full` mode. It runs every critical check and adds read-only Bybit Spot connectivity, market-data freshness, production status/readiness, dependency, artifact, schedule, and infrastructure checks.
- Manual dispatch selects `critical` or `full`. `send_test_alert` and `send_test_summary` send test messages without writing monitor or notification-deduplication state.

Critical transitions alert immediately. An unchanged incident is suppressed on later hourly runs, and a single `RECOVERED` message is emitted when that component returns to healthy. A complete `BTC DCA SYSTEM HEALTH` summary is sent at most once per UTC day.

The monitor is read-only: it has `contents: read` and `actions: read`, runs on `ubuntu-24.04`, never submits an order, and preserves `live_execution_enabled: false`, `kill_switch: true`, `order_submission: not_implemented`, and `Authorization: NOT_AUTHORIZED` as critical safety invariants.

GitHub Actions APIs cannot prove that a self-hosted runner is currently online with these permissions. The runner check therefore reports recent activity from the expected scheduled shadow job, and treats missing/stale/queued evidence as unhealthy; a historical `runner_name` is not treated as indefinite online proof. Market-data freshness likewise requires a recent retained Bybit Spot BTCUSDT artifact; generic connectivity is insufficient.

The legacy `production-shadow-watchdog.yml` and `bybit-contract-watch.yml` remain present during cutover. Their behavior is covered alongside the unified monitor. They are safe to delete only after the unified workflow is enabled and its first scheduled runs are observed, because the unified workflow now owns their expected-slot, contract-drift, and delivery-deduplication responsibilities.
