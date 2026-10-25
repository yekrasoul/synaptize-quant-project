# Legacy to V2 cutover parity

This is offline evidence for the safe pre-network boundary. It compares
invariants for the same canonical V1 decision and does not establish live
readiness or authorize a writer.

| Legacy semantic | V2 semantic | Status | Evidence |
| --- | --- | --- | --- |
| Bybit Spot BTCUSDT | Bybit Spot BTC/USDT | PARITY | `test_cutover_parity.py` matrix |
| Buy / Market | BUY / MARKET | PARITY | canonical intent and mapper |
| quote amount in `qty` | quote amount in CCXT amount | PARITY | `marketUnit=quoteCoin` |
| `isLeverage=0` | `isLeverage=0` | PARITY | legacy request and V2 mapper |
| deterministic client identity | deterministic client identity | PARITY | decision/run identity tests |
| kill switch and monthly cap blocks | kill switch and monthly cap blocks | PARITY | legacy safety and V2 Risk tests |
| duplicate/ambiguous requires reconciliation | duplicate/ambiguous requires reconciliation | PARITY | no-resubmit recovery test |
| reconcile by client identity | reconcile by client identity, venue ID supplementary | PARITY | reconciliation identity row |

The legacy approval/activation ceremony is not treated as a separate V2
business invariant. V2 replaces that ceremony with the same fail-closed
policy, identity, capital, venue, and reconciliation controls; it remains
`TARGET_V2` and is not a production cutover.

Legacy `LiveOrderEngine`, `SignedBybitSubmissionTransport`, canary execution,
and historical reconciliation/ledger readers remain retained. This document
does not remove or route around them.
