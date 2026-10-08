# Production Operations Runbook

## Daily observe-only check

Run these read-only commands:

```bash
btc-dca production-status --json
btc-dca production-blockers --json
btc-dca contract-status --json
btc-dca preauthorization-status --json
```

Expected current state: software health
`HEALTHY_BLOCKED_EXTERNAL_DEPENDENCY`; production readiness `NOT_READY`;
preauthorization `BLOCKED`; blocker `BYBIT_QUOTE_UNIT_MAX_NOT_EXPOSED`; and
authorization `NOT_AUTHORIZED`. A production block is not permission to work
around a failed gate.

The `btc-dca` spelling is the operator-installed command wrapper. In a source
checkout without that wrapper, run the equivalent as
`uv run python -m btc_dca_bridge <command> ...` from `btc_dca_bridge/`.

## Immutable checkpoint

Capture an append-only status snapshot when an operational checkpoint is
needed:

```bash
btc-dca collect-production-status --json
```

Snapshots are status history, not spend or authorization records. Alert
artifacts are derived from adjacent snapshots and can be reconstructed if a
process stops after snapshot publication.

## History

```bash
btc-dca production-status-history --limit 20 --json
```

## Reconciliation

Unresolved operations take precedence over the external contract blocker. If
any prior submission may have reached Bybit, do not prepare or submit a new
order for that identity. Never retry or top up. Use only the existing
`reconcile-existing` recovery path for the exact prior identity; it is a
no-POST recovery operation. Do not edit the canonical ledger manually.

## External contract blocker

`BYBIT_QUOTE_UNIT_MAX_NOT_EXPOSED` means the supported Bybit contract does not
currently provide an approved, authoritative quote-denominated maximum for a
BTCUSDT Spot Market Buy sized with `marketUnit=quoteCoin`. No base-quantity ×
price estimate, wallet arithmetic, pre-check POST, or other substitute is
approved. **No safe workaround is approved.** Production execution remains
disabled while this blocker is active.

## Future activation boundary

A future real-money canary requires every step below, in order; no step may be
skipped:

1. An authoritative quote-unit maximum becomes available in an official Bybit contract.
2. The official source and its exact semantics are researched.
3. The endpoint and field receive manual review for the precise Spot operation.
4. The production quote-limit policy is explicitly updated in reviewed source code.
5. The shared validator and tests are updated and pass.
6. The change is reviewed and merged.
7. Fresh, build- and host-bound production evidence is collected.
8. Production readiness reports ready.
9. Preauthorization reports ready.
10. The user separately provides explicit real-money authorization for the exact canary.

Evidence, readiness, credentials, a canary manifest, or a software merge is
not authorization. This runbook stops before approval or execution.
