# Production Shadow Monitoring Incident Diagnosis

Baseline frozen on 2026-10-24.  No execution, authorization, or order path was
invoked while collecting this evidence.

## Baseline

- Repository: `yekrasoul/synaptize-quant-project`
- Fetched `origin/main`: `f5f8a6f6276f3fd714a7224516c00ea8fc692cf3`
- Working branch: `audit/monitoring-architecture-stabilization`
- Initial working tree: clean.
- Initial test invocation: `uv run pytest -q` could not start because `pytest`
  was absent from the current locked environment. A locked dependency sync is
  required before the test baseline can be completed.

## Verified incident facts

1. GitHub Actions run `40033068129` (Production shadow watchdog) was scheduled,
   created and started at `2026-10-23T22:21:26Z`, and failed at its inspection
   step. Its log records:
   - expected slot: `2026-10-23T18:23:00Z`
   - matched run: none
   - status: missing
   - reason: "Expected scheduled run was not observed during the one-hour grace
     interval."
2. GitHub Actions run `40027487013` (Production shadow) was a successful
   scheduled run created at `2026-10-23T21:32:13Z`, completed at
   `2026-10-23T21:33:09Z`, and completed its read-only shadow, immutable
   artifact retention, notification, and workflow-summary steps.
3. The successful run's canonical logical context and retained artifact use
   `run_20261023T182300Z_scheduled_c52f06f3462e`. This is evidence that the
   production process associated the delayed GitHub delivery with the 18:23Z
   schedule slot. It also records `no_order_executed: true`.
4. The watchdog implementation filters scheduled main-branch workflow runs by
   API `created_at` in `[expected_slot, expected_slot + 1 hour)`. A 21:32Z run
   cannot pass the 18:23Z--19:23Z filter, even when it is the delayed 18:23Z
   production run. This is the direct cause of the false missing classification.
5. GitHub Actions documents that scheduled events can be delayed under load and
   that queued jobs can be dropped. GitHub cron is consequently an intended
   trigger cadence, not a hard-real-time delivery or exact identity guarantee.
6. GitHub Actions run `39919012810` (System health monitor) failed in evidence
   collection. The log shows a `502 Server Error` from
   `GET /actions/runs/38683974418/jobs?per_page=100`. The workflow fetched jobs
   for every historical scheduled shadow run, so one transient failure prevented
   all subsequent health evaluation.
7. GitHub Actions run `40032500146` completed successfully while its workflow
   summary stated `Overall: CRITICAL`. Current behavior therefore conflates a
   successful monitoring execution with a critical observed system state in a
   way that is not explicit to operators.
8. `astral-sh/setup-uv@v6` looks only at the repository root for `uv.toml` or
   `pyproject.toml`, while this project file is at `btc_dca_bridge/pyproject.toml`.
   The action therefore falls back to its latest uv version. This is a
   reproducibility defect, independent of the monitoring incident.

## Root-cause conclusions

- **False watchdog alert:** confirmed. The watchdog models schedule identity as
  `created_at` near a nominal cron timestamp. That model is incompatible with
  GitHub's documented delayed delivery behavior.
- **Evidence fan-out failure:** confirmed. The system-health workflow makes an
  unbounded-per-history jobs request and has no retry/isolation policy for
  transient GitHub API failures.
- **Semantic ambiguity:** confirmed. A successful workflow can represent a
  failed health observation, but neither monitor currently uses an explicit
  `MONITOR_EXECUTION_STATUS`, `SYSTEM_HEALTH_STATUS`, and
  `SCHEDULER_HEALTH_STATUS` contract.

## Working hypotheses to test before implementation

1. The right scheduler signal is the latest relevant *scheduled* production
   shadow's freshness relative to cadence plus an explicit lateness allowance
   and workflow timeout, rather than an exact cron-slot match on `created_at`.
2. Recent relevant run/job evidence can be bounded to the candidate selected by
   schedule policy; historical job details are unnecessary for health
   classification.
3. The watchdog and system-health workflow can share a Python monitoring domain
   without altering Adaptive DCA V1 or live-execution behavior.

## Proposed monitoring convention, pending test characterization

- `MONITOR_EXECUTION_STATUS`: failed only when the monitor cannot collect or
  evaluate the required evidence reliably.
- `SYSTEM_HEALTH_STATUS`: structured HEALTHY/DEGRADED/ALERT/UNKNOWN result for
  the observed system.
- `SCHEDULER_HEALTH_STATUS`: structured result of the canonical freshness
  policy.
- A monitor workflow succeeds for a completed evaluation (including observed
  ALERT/UNKNOWN), and fails only for monitor-execution/evidence failure. Alerts
  and summaries carry the observed health separately. Relevant evidence gaps
  must be UNKNOWN/DEGRADED, never HEALTHY.

## Scope guard

This diagnosis does not authorize any order, canary execution, authorization,
or change to DCA economics, the monthly cap, allocation/sentiment policy,
Spot-only policy, leverage/borrowing prohibition, reconciliation, or retry
semantics.
