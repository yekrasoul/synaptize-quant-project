# Legacy to V2 cutover parity

This is offline evidence for the safe pre-network boundary. It compares
invariants for the same canonical V1 decision and does not establish live
readiness or authorize a writer.

The matrix is failure-sensitive: directly comparable rows derive `PARITY` or
`MISMATCH` from normalized values, and a negative-control test proves an
altered quote amount produces `MISMATCH`.

| Legacy semantic | V2 semantic | Status | Evidence |
| --- | --- | --- | --- |
| Bybit Spot BTCUSDT | Bybit Spot BTC/USDT | PARITY | derived normalized market comparison |
| Buy / Market | BUY / MARKET | PARITY | derived side/order-type comparison |
| quote amount in `qty` | quote quantity in CCXT amount | PARITY | derived Decimal comparison; negative control detects mismatch |
| `marketUnit=quoteCoin` | `marketUnit=quoteCoin` | PARITY | derived mapper comparison |
| `isLeverage=0` | `isLeverage=0` | PARITY | derived integer comparison |
| deterministic client identity | deterministic V2 client identity | INTENTIONAL_V2_REPLACEMENT | encodings differ; deterministic generation plus duplicate/reconciliation behavior is executed |
| persisted duplicate identity blocks/resolves | OMS duplicate identity blocks/resolves | PARITY | both paths exercised; no new V2 submit |
| ambiguous identity requires reconciliation | AMBIGUOUS recovers by reconciliation only | PARITY | both paths exercised; V2 submit count remains zero |
| deterministic HTTP rejection is terminal | DeterministicVenueRejection -> REJECTED | PARITY | executable terminal-rejection characterization |
| reconcile by client identity with no venue ID | reconcile(intent, None) preserving client identity | PARITY | both reconcilers are invoked with canonical client identity |

The legacy approval/activation ceremony is not treated as a separate V2
business invariant. V2 replaces that ceremony with fail-closed policy,
identity, capital, venue, and reconciliation controls. That is an
`INTENTIONAL_V2_REPLACEMENT`, not permission to activate execution.

Legacy `LiveOrderEngine`, `SignedBybitSubmissionTransport`, canary execution,
and historical reconciliation/ledger readers remain retained. Frozen
authenticated-evidence blockers and the activation checklist are unchanged.
