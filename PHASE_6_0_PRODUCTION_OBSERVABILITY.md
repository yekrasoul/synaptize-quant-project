# Phase 6.0 — Production Observability, State-Change Alerts & Audit Baseline

## Purpose and safety boundary

Phase 6.0 provides a read-only view of software health, production readiness,
preauthorization, the Bybit contract blocker, account/read evidence, reconciliation,
monthly spend, build identity, and checked-in execution safety. It implements
`observe → compare → report → alert` only.

It never authorizes, approves, prepares, submits, retries, or schedules an order.
The immutable status and alert artifacts are operational records, not spend
records. The canonical execution ledger remains the sole source of confirmed
spend. Real-money authorization is always persisted and displayed as
`granted: false`, `source: none`, `required: true`, `status: NOT_AUTHORIZED`.

Current expected contract state remains:

```text
software_health = HEALTHY_BLOCKED_EXTERNAL_DEPENDENCY
production_readiness = NOT_READY
preauthorization = BLOCKED
real_money_authorization = NOT_AUTHORIZED
external_blocker = BYBIT_QUOTE_UNIT_MAX_NOT_EXPOSED
```

The status service consumes the Phase 5.6 readiness, Phase 5.5 operations,
Phase 5.7 evidence, and Phase 5.9 blocker/capability components; it does not
reimplement their safety decisions.

## Commands

- `btc-dca production-status [--json]`: evaluate the current state without
  persisting a status snapshot.
- `btc-dca collect-production-status [--json]`: evaluate and atomically persist
  one immutable snapshot. If a new material state transition is found, persist
  its deduplicated alert first, then attempt configured Telegram delivery. A
  delivery result is not source-of-truth and cannot change readiness.
- `btc-dca production-status-history [--limit N] [--json]`: read recent snapshots
  in deterministic newest-first order.

The commands have no order submission transport path. Read checks reuse the
existing read-only Bybit clients. Collection writes only beneath
`data/production_status/` and `data/production_status_alerts/`; it never writes
the canonical ledger, attempts, approvals, or canary artifacts.

## Snapshot lifecycle and integrity

Snapshots use schema version `6.0.0` at
`data/production_status/YYYY/MM/DD/<snapshot-id>.json`, with a SHA-256 sidecar.
`ArtifactStore` validates schema and canonical JSON, publishes with atomic
no-clobber semantics, fsyncs data and directory metadata, and verifies the
read-back digest. Existing snapshots cannot be overwritten. Alert artifacts
have their own strict schema and immutable path. Status artifacts contain
sanitized states and identifiers only, never raw private API responses or
credential values.

Alerts are deterministic derivatives of adjacent, digest-verified snapshots.
Collection first repairs missing alerts across its bounded recent history,
then persists a new snapshot, then persists the transition alert. A failure in
the final step leaves the snapshot untouched and the next collection derives
the same alert identity from that persisted pair. `validate_status_artifacts`
reports such a gap as `incomplete` with `missing_alert_count`; it is not
snapshot corruption. Existing alerts and snapshots are never rewritten.

Monthly spend and remaining budget are computed from validated canonical,
confirmed ledger executions for the current UTC calendar month. Decisions,
manifests, approvals, attempts, alerts, and unresolved partial fills do not
become confirmed spend through observability.

## State precedence

The aggregate operator state uses this precedence:

```text
CORRUPT
> ACTION_REQUIRED (unsafe config or failed/unavailable required checks)
> RECONCILIATION_REQUIRED
> EXTERNAL_DEPENDENCY_BLOCKED
> HEALTHY_OBSERVE_ONLY
```

This means an external Bybit contract limitation cannot hide a corrupt ledger,
unsafe execution configuration, stale/unavailable evidence, or unresolved
submission. Reconciliation remains primary when unresolved work coexists with
the external quote-limit blocker; the blocker is retained as supplemental
context.

`overall_operator_state` is never `AUTHORIZED`, `EXECUTE`, or `BUY`. The
production readiness and preauthorization values are copied from the existing
gates, not inferred from lack of alerts.

## Snapshot comparison and alerts

Two immutable snapshots are compared deterministically. Snapshot IDs, capture
timestamps, digest metadata, and volatile blocker observation timestamps do
not create material changes. Contract capability comparison uses Phase 5.9's
offline comparison and ignores verification-date churn.

Classifications:

| Classification | Typical trigger | Alert class |
| --- | --- | --- |
| `NO_MATERIAL_CHANGE` | No meaningful field changes | No alert |
| `INFO_CHANGE` | Commit or confirmed monthly spend changed | `INFO` |
| `ATTENTION_REQUIRED` | Required read/evidence degraded | `WARNING` |
| `SAFETY_REGRESSION` | Unsafe config, corruption, or newly unresolved submission | `CRITICAL` |
| `RECOVERY_PROGRESS` | Previously unresolved reconciliation resolved | `RECOVERY` |
| `EXTERNAL_DEPENDENCY_CHANGE` | Blocker/capability identity changed | `EXTERNAL_CHANGE` |

The immutable alert key is derived from previous snapshot digest, current
snapshot digest, classification, and sorted changed fields. Existing alert
artifacts are checked before publication. Identical snapshots produce no
alert; only alert artifacts newly created during the current collection are
Telegram delivery candidates. A failed or interrupted delivery does not remove
the alert, cause snapshot rewrite, or trigger a later automatic resend.
Telegram delivery is best-effort notification only and is not the deduplication
store or a source of operational truth.

Telegram content is a short, sanitized state summary. It contains no API keys,
signatures, authorization headers, raw private responses, or notification
tokens. A notification delivery failure cannot alter the snapshot or ledger.

## External contract behavior

The quote-unit maximum remains unexposed and unapproved. Phase 5.9's blocker
evaluator remains authoritative, so a status snapshot reports the external
dependency without changing the approved quote-limit source policy. A changed
capability snapshot can raise `EXTERNAL_DEPENDENCY_CHANGE`; it cannot approve a
source, remove execution gates, or grant authorization. A human-reviewed code
change, new production evidence, and a separate explicit authorization process
remain necessary.

## Operator procedure

1. Run `production-status --json` to inspect the current state.
2. If state is `CORRUPT`, stop and investigate integrity before other work.
3. If state is `ACTION_REQUIRED`, address the named internal/config/evidence
   issue without changing execution policy.
4. If state is `RECONCILIATION_REQUIRED`, use the existing recovery path; do not
   submit a new order.
5. If state is `EXTERNAL_DEPENDENCY_BLOCKED`, preserve observe-only operation;
   do not derive a quote maximum or bypass the blocker.
6. Use `collect-production-status --json` when an immutable operational
   checkpoint and state-change comparison are needed.
7. Use `production-status-history --limit N --json` to inspect prior snapshots.

No workflow or schedule is introduced by this phase. Production execution
remains disabled, and the observability commands cannot create approval or
submission artifacts.
