# Bybit/CCXT V2 Cutover

Baseline: `4c54cbe7038c0d273050668cbf884ea409b57251`.

## Decision

**BLOCKED.** `runtime_execution_policy.yaml` remains `execution_enabled: false`
with its kill switch active. No live endpoint was invoked to produce this
report.

## Observable contract evidence

| Safety contract | Evidence | Status |
| --- | --- | --- |
| Spot BTC/USDT identity | Legacy `SpotMarketBuyRequest` fixture and V2 mapper test | VERIFIED |
| Quote-denominated market buy | `qty=quote amount`, `marketUnit=quoteCoin` fixture parity | VERIFIED |
| No leverage | Legacy `isLeverage=0` fixture parity | VERIFIED |
| Client identity | `orderLinkId` fixture parity and identity reconciliation test | VERIFIED |
| Native minimum / quote precision | Adapter fixture tests before claim/POST | VERIFIED |
| Acknowledgement, status, fills, fees | CCXT normalization fixtures, including client-identity recovery | VERIFIED |
| Deterministic reject versus uncertain post | CCXT `InvalidOrder` and timeout fixtures | VERIFIED |
| Exact signed HTTP method/path/body/headers | No safe controlled write capture | **NOT VERIFIED** |
| Credential scope and intended production account | No production credentials used | **NOT VERIFIED** |

The trusted legacy fixture establishes the expected Bybit V5 observable
request fields: `category=spot`, `symbol=BTCUSDT`, `side=Buy`,
`orderType=Market`, quote `qty`, `marketUnit=quoteCoin`, `isLeverage=0`, and
`orderLinkId`. CCXT uses the normalized `BTC/USDT`, `buy`, `market` call plus
those Bybit parameters. CCXT's final signed-wire transformation is not treated
as proven by a unit fixture.

## Submit-path matrix

| Current owner | V2 replacement | Parity / removal status |
| --- | --- | --- |
| `live_order.SignedBybitSubmissionTransport` and `LiveOrderEngine` | `ExecutionService -> CcxtVenueAdapter.submit_once` | Keep: wire parity and single-path cutover not verified |
| `bybit_spot_transport.BybitSpotRestTransport` | `CcxtVenueAdapter` | Keep: legacy behavior reference until controlled parity capture |
| `private_bybit.BybitPostAckReconciler` | `CcxtVenueAdapter.reconcile` by `orderLinkId` | Keep: live response parity not verified |
| `bybit_submission.BybitSubmissionReconciler` | V2 reconciliation + confirmed accounting | Keep: historical/current readers may still be required |
| `execution_v2.ExecutionService` | Same | Candidate canonical path; currently blocked by runtime policy |
| Production Shadow | `plan_v2_shadow` pre-network mapping/risk path | Active read-only validation; creates no claim and never POSTs |

## Canonical durable evidence

`execution_record_v2` is the schema-validated, secret-free projection of the
durable OMS record. It carries canonical/client identity, decision and
strategy linkage, venue/market/account/environment identity, lifecycle,
claim state, venue order identity, ambiguity and reconciliation references.
The repository remains the single mutable lifecycle source; the immutable
artifact is an audit projection, not a competing state machine.

Accounting follows only `venue fill evidence -> reconciliation -> CONFIRMED
ExecutionRecord -> LedgerEntry`. It is idempotent by confirmed execution
identity and never posts acknowledgement-only data. Historical JSONL ledger
records remain immutable/readable and continue to supply V1 monthly-spend
projection.

## Removal criteria

Legacy submitters/reconcilers may be removed only after all checklist entries
in `config/v2_activation_checklist.yaml` are `VERIFIED`, a controlled
non-production account proves final wire/response parity, and routing leaves
exactly one reachable production network-write path. Historical schema readers
are not removal candidates merely because current code no longer emits them.
