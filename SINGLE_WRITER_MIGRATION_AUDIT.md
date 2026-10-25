# Single-Writer Migration Audit

Baseline: `03e5c018b3b7cb416e418c07080c36cedd13e89c` (canonical `main`).

This audit records the migration plan and historical responsibilities. Its
original component matrix and Gate Reality snapshot predate the completed V2
single-writer cutover and are not current reachability evidence. Current state:
the legacy `canary-execute` writer is permanently retired, `btc-dca execute`
routes only to V2 after gates, and the activation checklist marks
`one_reachable_production_submit_path: VERIFIED`. Activation remains
`BLOCKED` because account/credential evidence is incomplete and Production
Shadow V2 pre-network evidence is only partially verified. See the
[current V2 cutover record](BYBIT_CCXT_CUTOVER.md) and
[activation checklist](btc_dca_bridge/config/v2_activation_checklist.yaml).

## Classification rules

Each component has one architecture label:

- `CURRENT_CANONICAL`: current production/shadow safety or historical truth
  that must remain authoritative today.
- `TARGET_V2`: the intended V2 replacement or destination component; existence
  and unit evidence do not make it production-reachable.
- `LEGACY_READ_ONLY`: retained only for reads, reconciliation, or historical
  compatibility and cannot submit.
- `DEAD`: no reachable runtime caller remains; deletion still requires checking
  public/test compatibility.
- `TRANSITIONAL`: still owns a real current invariant while V2 is intended to
  replace it. It must not be removed until the stated evidence exists.

## Responsibility and reachability matrix

| Component | Classification | Current responsibility | Production reachability | V2 replacement / evidence | Removal readiness |
| --- | --- | --- | --- | --- | --- |
| V1 Adaptive DCA strategy / `StrategyDecision` | `CURRENT_CANONICAL` | Computes the unchanged BTC DCA economics and decision artifacts. | Reachable through `run --mode shadow` and scheduled Production Shadow. | V2 consumes the same decision as input; V1 economic fixtures and canonical validation pass. | Keep; V1 economics are not being migrated. |
| Risk Engine | `TARGET_V2` | Evaluates final mutable safety context, account identity, capital, market, leverage, borrowing, kill switch, and venue health. | Library/test composition only; `btc-dca execute` is fail-closed before construction. | Must be composed in the production V2 entrypoint with fresh context immediately before claim. | Not removable or promotable until operational composition is proven. |
| Runtime kill switch / execution policy | `TRANSITIONAL` | Checked-in policy blocks both legacy and V2 execution (`execution_enabled: false`, kill switch active). | Reachable as a blocking control in CLI and legacy gates. | One authoritative policy snapshot must feed V2 Risk and the final pre-write gate. | Keep until V2 composition proves one source and one final re-read. |
| `VenueCircuitBreaker` | `TARGET_V2` | Tracks venue failure/degraded/open/recovery state; Risk consumes current state. | Tested/library composition only. | Fresh breaker state in V2 Risk immediately before OMS claim. | Keep; operational V2 evidence still required. |
| `OrderIntent` creation | `TARGET_V2` | Converts a canonical decision into an immutable BTC/USDT Spot intent and client identity. | V2 shadow/pre-submit and planning paths; no live writer composition. | `StrategyDecision → OrderIntent` in `btc-dca execute`; identity must not be regenerated after claim. | Not removable; verify production composition first. |
| OMS / durable execution repository | `TARGET_V2` | Owns lifecycle identity, durable pre-network claim, duplicate suppression, and crash recovery. | Library/test composition only. | Canonical V2 OMS before EMS/network write. Durable claim and recovery evidence are verified. | Not removable; must be in the sole production path. |
| `ExecutionService` / EMS | `TARGET_V2` | Runs final Risk, claims once, submits once, classifies rejection/ambiguity, reconciles, and persists lifecycle. | No current CLI/workflow composition reaches it. | Sole EMS in future `btc-dca execute`; service tests cover claim, ambiguity, reject, and reconciliation behavior. | Not removable; production composition and operational evidence remain. |
| `CcxtVenueAdapter` / CCXT | `TARGET_V2` | Maps and prepares Bybit Spot BTC/USDT quote market buys and performs venue calls. | No current production composition reaches `submit_once`. | Sole future adapter; offline signed-wire characterization is verified, authenticated response parity is not. | Not removable; requires operational shadow and cutover evidence. |
| Reconciliation | `TRANSITIONAL` | Legacy read client/reconciler supplies current Bybit order/fill reads; V2 adapter also defines client-identity reconciliation. | Legacy read path is available; V2 reconciliation is library-only. | V2 `VenueAdapter.reconcile` by canonical/client identity, with confirmed outcome before accounting. | Keep legacy reads until authenticated response parity and V2 operational reconciliation are proven. |
| Accounting | `TRANSITIONAL` | Legacy JSONL ledger remains historical confirmed truth; V2 repository accepts confirmed reconciled entries only. | Legacy ledger readers and V1 monthly projection are reachable; V2 accounting repository is library-only. | One V2 confirmed-reconciliation-to-accounting path plus compatibility reader for existing history. | Keep until history migration/reader compatibility and unified V2 consumption are explicitly proven. |
| Monthly spend projection | `TRANSITIONAL` | V1 uses canonical legacy ledger history; unified projection can combine legacy confirmed records and confirmed V2 entries. | Reachable in strategy/Risk and readiness paths. | V2 consumes the unified confirmed projection without changing the $500 policy. | Keep both history compatibility and unified projection until cutover validation. |
| `LiveOrderEngine` | `TRANSITIONAL` | Owns legacy approval/activation checks, fresh private reads, pre-POST durable attempt evidence, ambiguity handling, and confirmed ledger append. | Only through controlled canary artifacts; blocked by committed policy and activation state. | `ExecutionService` + OMS + V2 reconciliation/accounting. | Keep temporarily; all listed invariants require operational V2 replacement evidence. |
| `SignedBybitSubmissionTransport` | `TRANSITIONAL` | Legacy authenticated HMAC `POST /v5/order/create` writer with no retry and ambiguous-outcome handling. | Reachable only behind the legacy controlled activation chain; blocked on canonical main. | `CcxtVenueAdapter → CCXT` as sole writer. | Delete only after V2 is production-composed, response/account parity is proven, and no legacy caller remains. |
| `BybitSpotRestTransport` | `TRANSITIONAL` | Legacy direct transport owns request gating, durable pre-POST journal requirements, Spot-only endpoint enforcement, and read reconciliation adapter. | No current CLI/workflow caller; public/library and tests retain the capability. | V2 OMS/EMS/adapter and V2 reconciliation. | Keep temporarily because its tested safety contract and compatibility role are not yet replaced. |
| `canary-execute` | `TRANSITIONAL` | Historical controlled execution composition and artifact-bound activation boundary. | Fail-closed on main; no order is reachable under current policy. | Replace with canonical V2 `btc-dca execute` after explicit cutover. | Keep until the V2 route proves all equivalent gates and legacy route is disabled/deleted in a separate cutover. |
| `live-submit` | `DEAD` | Historical command surface; always returns a block and constructs no transport. | Fail-closed; no writer. | V2 `btc-dca execute`. | Safe candidate for removal during the cutover PR after CLI compatibility review. |
| Production Shadow | `CURRENT_CANONICAL` | Runs public Bybit market data, sentiment, V1 decision, immutable artifacts, and V2 pre-submit planning without claim/POST/accounting mutation. | Scheduled and manual workflow reachable; read-only. | Remains the read-only operational proving path for V2 pre-network parity. | Keep; it is an active safety/evidence boundary. |
| System Health Monitor | `CURRENT_CANONICAL` | Reads workflow/artifact/ledger health and sends operational notifications; does not submit Bybit orders. | Scheduled/manual workflow reachable; Bybit portion read-only. | Continue monitoring the canonical shadow and cutover state. | Keep; not an order writer. |
| Private Bybit read client | `LEGACY_READ_ONLY` | Narrow authenticated account/order/fill reads for evidence and reconciliation; no order/cancel/withdraw/transfer methods. | Read-only where invoked by evidence/readiness/reconciliation paths. | Fresh read context provider for V2 Risk and reconciliation. | Keep until V2 uses an equivalent narrow read port; never broaden to make a gate green. |

## Old architecture that remains canonical

The following are category A, **KEEP TEMPORARILY**, dependencies:

1. The checked-in execution policy and kill switch still provide the only
   committed fail-closed production control. V2 must consume one authoritative
   source and re-read it immediately before the durable claim.
2. The legacy ledger remains the canonical historical source for V1 monthly
   spend. V2 accounting is additive and confirmed-only; it is not permission
   to discard historical ledger readers.
3. `LiveOrderEngine` and its direct transport still own the legacy controlled
   path's artifact-bound approval, fresh private checks, pre-POST attempt
   persistence, ambiguity handling, and confirmed-ledger boundary.
4. Legacy private reads and reconciliation remain necessary for historical
   evidence and compatibility until V2 authenticated response parity is
   demonstrated.

No category B migration is safe in this task because V2 is not yet the
canonical production composition. `live-submit` is the only category C
candidate: it is an always-blocked command with no writer, but its removal is
best bundled with the explicit cutover and CLI compatibility decision.

## Future sole production writer contract

```text
btc-dca execute
  → canonical V1 StrategyDecision
  → OrderIntent
  → fresh RiskContext and RiskAssessment
  → one authoritative kill switch
  → fresh VenueCircuitBreaker state
  → fresh authenticated account context
  → OMS durable claim
  → ExecutionService
  → CcxtVenueAdapter
  → CCXT Bybit Spot BTC/USDT
  → reconciliation by canonical/client identity
  → confirmed-only accounting
```

Before this path can become `CURRENT_CANONICAL`, all of the following must be
proven with actual evidence, not implementation existence:

- the CLI composition reaches exactly this path and no direct transport;
- the legacy canary and direct transport route are unreachable or removed;
- the final mutable Risk, kill-switch, venue-health, account, balance, and
  unresolved-identity state is fresh immediately before the OMS claim;
- exactly one durable claim precedes the only order-create network boundary;
- duplicate identities never regenerate or resubmit;
- deterministic rejects are terminal, while uncertain outcomes reconcile and
  never retry blindly;
- authenticated response/fill/fee parity is sufficient for V2 reconciliation;
- accounting accepts only confirmed reconciled effects and preserves V1
  historical truth/monthly economics;
- Spot BTC/USDT, quote-denominated market buy, no leverage, and no borrowing
  remain enforced;
- account/environment identity, withdrawal exclusion, operational Production
  Shadow, and single-writer reachability are all evidenced.

## Test cleanup audit

The current tests protect meaningful invariants: durable pre-network
persistence, process-safe identity claims, ambiguity recovery, deterministic
rejection, final fresh Risk, kill switch, venue circuit breaker, Spot-only
mapping, signed-wire structure, reconciliation, confirmed-only accounting,
read-only evidence, and blocked activation. They should be retained.

Tests that assert the legacy command/artifact surface should be migrated or
removed only in the explicit cutover PR, after their invariant is represented
by V2. In particular, do not remove legacy transport tests merely because the
implementation is old: the current transport still protects a real pre-POST
journal and ambiguity contract. The `live-submit` always-blocked surface is a
candidate for cleanup, but no cleanup is required to establish this audit.

## Gate reality

The checklist values below describe the state when this audit was written and
are superseded by the current activation checklist linked above:

```text
account_environment_identity              NOT_VERIFIED
credential_scope_withdrawal_exclusion    NOT_VERIFIED
production_shadow_v2_pre_network         PARTIALLY_VERIFIED
one_reachable_production_submit_path     NOT_VERIFIED
activation_decision                      BLOCKED
```

The repository therefore has no safe basis for a production cutover in this
task. No real-money action, network order-create, cancellation, withdrawal,
transfer, activation, kill-switch disablement, runtime enablement, or ledger
mutation was performed.

## Recommended next PR

The next PR should be the explicit cutover PR only after the remaining
authenticated and operational evidence is available. It should compose the
V2 path, remove or permanently disable the legacy production writer routes,
retain read-only historical compatibility, add a sole-writer regression test,
run the complete validation suite, and leave activation blocked unless every
activation requirement is independently verified.
