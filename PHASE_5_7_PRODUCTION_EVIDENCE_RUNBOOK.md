# Phase 5.7 Production Evidence Runbook

This runbook is read-only and stops before approval or execution.

1. Update local `main`.
2. Verify the exact repository commit.
3. Run `btc-dca production-connectivity`.
4. Run `btc-dca production-readiness`.
5. Run `btc-dca collect-production-evidence --json`.
6. Run `btc-dca verify-production-evidence <evidence-id> --json`.
7. Run `btc-dca preauthorization-status --json`.
8. Stop.

No command in this runbook creates an approval, prepares a canary, submits an order,
or grants real-money authorization. A valid evidence bundle is necessary but not
sufficient for a real-money canary.

Evidence is immutable, expires after 10 minutes, and is host-bound. Dynamic wallet,
availability, and clock evidence is limited to 60 seconds; account metadata to 5 minutes;
instrument metadata to 10 minutes. A changed repository commit or host requires fresh
collection. Evidence storage is operational truth only and cannot mutate the canonical
execution ledger.

```text
real_money_authorization:
  granted: false
  source: none
  required: true
  status: NOT_AUTHORIZED
```

**Valid production evidence is necessary but not sufficient. It cannot authorize a real
order. Merging Phase 5.7 does not authorize a real BTC order.**
