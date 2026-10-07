# Phase 5.6 Activation Runbook

This is documentation only. No step below is authorized or executed by Phase 5.6.

**MERGING PHASE 5.6 DOES NOT AUTHORIZE A REAL BTC ORDER.**

Future separately authorized sequence:

1. Update local `main` and verify the exact deployment commit.
2. Run `btc-dca production-readiness --json`.
3. Require `READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION`.
4. Calculate a fresh V1 decision and prepare a fresh canary.
5. Inspect the exact amount, identity, manifest digest, and payload fingerprint.
6. Obtain separate explicit user authorization for that exact real-money order.
7. Create a short-lived persisted `LiveApproval` bound to the manifest.
8. Activate any future runtime gate only through a separately reviewed mechanism.
9. Run `canary-execute` exactly once.
10. Immediately run `reconcile-existing` and verify authoritative fills.
11. Verify the canonical ledger and exactly-once execution identity.
12. Restore and verify disabled production defaults.
13. Run `audit-run` and `ops-health`.

Phase 5.6 does not create a standing approval, permanent live configuration, scheduled execution, retry loop, or unattended workflow. Real-money authorization remains an external, explicit boundary:

```yaml
real_money_authorization:
  granted: false
  required: true
  status: NOT_AUTHORIZED
```

Exit codes remain `0` safe success, `2` blocked/not ready, `3` reconciliation required, `4` unavailable/ambiguous, and `5` corruption/integrity failure.
