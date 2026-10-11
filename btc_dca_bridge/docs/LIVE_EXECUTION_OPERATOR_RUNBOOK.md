# Controlled one-shot operator runbook

This procedure is read-only until the explicit ceremony commands are reached.
It cannot turn the checked-in execution policy live; the repository defaults
remain fail-closed.

1. Inspect the complete current chain:

   ```text
   btc-dca live-readiness --json
   ```

   Stop on any blocker. The report names the missing or invalid artifact and
   the next permitted command.

2. Prepare the exact V1 canary from the approved decision:

   ```text
   btc-dca canary-prepare --decision-json ... --run-id ... --month YYYY-MM
   ```

3. Review the persisted manifest, then approve the exact manifest SHA, amount,
   deterministic client order ID, and payload fingerprint:

   ```text
   btc-dca canary-approve --run-id ... --canary-id ... --manifest-sha ... \
     --amount ... --client-order-id ... --payload-fingerprint ...
   ```

   This creates `LIVE_EXECUTION_APPROVAL`; it is not authorization and submits
   no order.

4. If the current evidence and identities still match, perform the separate
   explicit authorization and short-lived activation ceremonies. Do not reuse
   expired or mismatched SHAs.

5. Prepare governance-only enablement from verified evidence if required by
   the deployment policy. It is not standing authorization and does not enable
   the checked-in config.

6. Run `live-readiness --json` again. Execute only when every persisted SHA,
   TTL, account identity, budget, liability, reconciliation, and duplicate
   guard is exact. An ambiguous exchange state blocks retry; reconcile first.

At every stop or failure, the required safety result is:

```text
No order submitted.
No ledger mutation.
No standing authorization.
No standing activation.
```
