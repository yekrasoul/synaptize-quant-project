# Scheduled production shadow (Phase 4)

Phase 4 operates the existing read-only pipeline on GitHub Actions and sends a
Telegram message from its structured result. It does not add an exchange order
client, authenticated exchange endpoint, ledger write, or portfolio mutation.

## Schedule and run time

`.github/workflows/production-shadow.yml` runs at `0 11 * * *` and also supports
`workflow_dispatch`. This workflow deliberately uses GitHub cron's default UTC
semantics rather than configuring a timezone: 11:00 UTC is 12:00 in London
while BST is active and 11:00 in London during GMT. Phase 4 accepts this
one-hour winter shift and does not claim that the fixed UTC schedule follows
Europe/London wall time.

GitHub exposes scheduled triggering and the Actions UI manual-dispatch entry
only when the workflow file exists on the default branch. This feature branch
is reviewable but deliberately not activated; both triggers become available
only after a separately approved merge reaches the default branch.

The scheduled slot is logical identity, not a fabricated retrieval time. The
application records the exact GitHub process start separately, then selects the
next minute boundary for acquisition and waits at most 60 seconds. That
minute-aligned acquisition context is passed to the unchanged market and
sentiment adapters. A late runner therefore retains the 11:00 logical slot and
deterministic run ID while its snapshots record the later canonical acquisition
minute. The market-data layer does not round timestamps.

Scheduled IDs use:

```text
run_<logical-slot-YYYYMMDDTHHMMSSZ>_scheduled_<12-char-SHA256-prefix>
```

The suffix hashes `scheduled|<RFC3339-slot>`. Manual runs use the original
workflow creation minute and hash the GitHub `run_id`, producing a `manual_`
suffix; they cannot accidentally claim a scheduled identity. A workflow rerun
keeps both values and therefore the same manual identity while separately
recording the new attempt's process start.

## Workflow boundaries

The workflow uses one non-cancelling concurrency group. A later invocation
waits rather than cancelling an active run that may already have immutable
partial evidence. It checks out the repository, installs Python and uv, runs
`uv sync --frozen`, verifies notification secrets, derives the run context, and
invokes only application CLI commands. Config, every schema, and the canonical
ledger are validated before public acquisition.

The production command composes the existing path only:

```text
Bybit BTCUSDT Spot direct
→ TradingView exact BYBIT:BTCUSDT Spot fallback
→ Alternative.me Crypto Fear & Greed
→ reconciled execution ledger
→ deterministic V1 engine
→ immutable artifacts and completed manifest
```

All decision values used by Telegram and the GitHub summary come from the
structured production result. Neither YAML nor the notification adapter
recalculates allocation.

## Retention and idempotency

The runner filesystem is ephemeral. A successful run uploads the complete
`data/` artifact tree, including every canonical JSON file and `.sha256`
sidecar, as `production-shadow-<run_id>` using `actions/upload-artifact`. The
upload must succeed before the success notification is attempted. Completed
artifacts are retained for 90 days; partial immutable evidence from failed runs
is uploaded under a distinct name for 30 days when present. Temporary files and
secrets are not included.

GitHub artifact retention is durable across runner loss but finite. It is an
explicit Phase 4 operational retention policy, not permanent archival storage.
Before a longer regulatory or audit horizon is required, the project must adopt
an approved append-only archive; Phase 4 does not add a database, cloud bucket,
or repository write token.

Before running, the workflow looks for the exact unexpired completed artifact
name and restores it. The immutable store then detects the completed manifest,
returns `already_completed`, and suppresses a second success message. Conflicts
and partial runs still fail closed and are never overwritten. Duplicate
suppression is guaranteed while the retained GitHub artifact exists; expiry is
the documented boundary of this Phase 4 mechanism.

## Telegram

Repository secrets required:

- `TELEGRAM_BOT_TOKEN`: Bot API credential used only in the HTTPS request path.
- `TELEGRAM_CHAT_ID`: target chat identifier used only in the request body.

No Bybit key or other exchange credential is used. Secrets are validated for
presence without printing their values. The notifier sends plain text, uses
explicit 3-second connect and 10-second read timeouts, and makes at most three
attempts with bounded exponential backoff. HTTP 429 and 5xx responses are
retried; persistent rate limiting, non-2xx status, timeout, connection failure,
and malformed success payloads raise typed notification errors. Error messages
never contain the bot token or chat ID.

A success message includes the canonical market source, price, 7D high,
drawdown, numeric Fear & Greed value, allocations, monthly state, final
purchase, and run ID. It ends with the shadow recommendation and `NO ORDER
EXECUTED`. A failed pipeline sends stage, typed category, source when available,
timestamp, run ID, and the same no-order statement without a traceback.

Notification is not canonical state. If delivery fails after a completed and
retained run, the job fails as `NOTIFICATION_FAILED` (or its more specific typed
cause), but the valid artifacts remain intact. A retention failure sends a
`DURABLE_RETENTION_FAILED` failure message and never sends a success message.

## Operations

Manual read-only production shadow:

1. Open the **Production shadow** workflow in GitHub Actions.
2. Choose **Run workflow** on the desired reviewed ref.
3. Confirm the job summary shows one run ID, retention success, Telegram sent,
   and `SHADOW — NO ORDER EXECUTED`.

For a failed workflow:

1. Inspect the concise stage/category in the job log and Telegram failure.
2. Preserve or download any `production-shadow-partial-*` evidence.
3. Correct only the operational cause; do not edit immutable artifacts.
4. Rerun the same GitHub workflow run for the same manual identity, or wait for
   the next scheduled slot. A retained completed run is restored and suppressed
   rather than notified twice.

An opt-in real Telegram probe is simply a manual workflow run with repository
secrets configured. Tests never use the network or require secrets.
