"""Disabled-by-default, single-shape Bybit Spot order engine.

The only POST capability in this module is the exact Spot BTCUSDT Market Buy
request. It is guarded by validated Phase 5.1 safety, Phase 5.2 read evidence,
an injected bounded approval, and an enabled test-only execution config.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol

from .artifacts import ArtifactStore, ArtifactType
from . import availability
from .quote_limits import PRODUCTION_QUOTE_UNIT_LIMIT_POLICY, QuoteUnitLimitPolicy
from .config import ExecutionConfig, load_strategy_config
from .errors import ArtifactAlreadyExistsError
from .execution import OrderIntent, SubmissionState, validate_execution_safety
from .ledger import append_execution_once
from .market_data.http import HttpResponse
from .private_bybit import ApiCredentialInfo, BybitSubmissionEvidence, CredentialClassification
from .schemas import validate_artifact, validate_live_approval

ORDER_CREATE_PATH = "/v5/order/create"
RECV_WINDOW = "5000"


class LiveOrderSafetyError(ValueError): pass
class AmbiguousSubmissionError(LiveOrderSafetyError): pass


def _utc(value: str) -> datetime:
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise LiveOrderSafetyError("approval timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed): raise LiveOrderSafetyError("approval timestamp must be UTC")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class LiveApproval:
    decision_id: str
    order_intent_id: str
    client_order_id: str
    approved_amount_usdt: Decimal
    approved_at_utc: str
    expires_at_utc: str
    approval_id: str
    canary_id: str
    manifest_sha256: str
    order_payload_fingerprint: str
    exchange: str = "Bybit"
    market_type: str = "spot"
    symbol: str = "BTCUSDT"
    side: str = "Buy"
    order_type: str = "Market"
    standing_authorization: bool = False

    @classmethod
    def for_manifest(cls, manifest: Mapping[str, Any], manifest_sha256: str, *, now_utc: datetime, approval_id: str | None = None) -> "LiveApproval":
        """Construct a five-minute, identity-bound approval in memory.

        Persisting this object is an explicit operator action; execution never
        manufactures an approval and no CLI creates one implicitly.
        """
        approved = now_utc.astimezone(UTC)
        canary = str(manifest["canary_id"])
        digest = approval_id or "approval-" + hashlib.sha256((canary + manifest_sha256).encode()).hexdigest()[:32]
        return cls(str(manifest["decision_id"]), str(manifest["order_intent_id"]), str(manifest["client_order_id"]), Decimal(str(manifest["approved_amount_usdt"])), approved.isoformat().replace("+00:00", "Z"), (approved + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"), digest, canary, manifest_sha256, str(manifest["order_payload_fingerprint"]))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "5.4.0", "approval_id": self.approval_id, "canary_id": self.canary_id, "manifest_sha256": self.manifest_sha256, "decision_id": self.decision_id, "order_intent_id": self.order_intent_id, "client_order_id": self.client_order_id, "approved_amount_usdt": str(self.approved_amount_usdt), "order_payload_fingerprint": self.order_payload_fingerprint, "exchange": self.exchange, "market_type": self.market_type, "symbol": self.symbol, "side": self.side, "order_type": self.order_type, "approved_at_utc": self.approved_at_utc, "expires_at_utc": self.expires_at_utc, "standing_authorization": self.standing_authorization}

    def validate(self, intent: OrderIntent, *, now_utc: datetime, manifest: Mapping[str, Any], manifest_sha256: str) -> None:
        if (self.decision_id, self.order_intent_id, self.client_order_id) != (intent.decision_id, intent.order_intent_id, intent.client_order_id):
            raise LiveOrderSafetyError("explicit live approval does not bind to the immutable OrderIntent")
        if self.approved_amount_usdt != intent.quote_amount_usdt: raise LiveOrderSafetyError("explicit live approval amount does not match OrderIntent")
        approved, expires = _utc(self.approved_at_utc), _utc(self.expires_at_utc)
        if expires <= approved or expires - approved > timedelta(minutes=5): raise LiveOrderSafetyError("approval TTL must be no more than five minutes")
        if now_utc < approved or now_utc >= expires: raise LiveOrderSafetyError("explicit live approval is expired or not yet valid")
        if self.standing_authorization: raise LiveOrderSafetyError("standing authorization is prohibited")
        expected = (manifest["canary_id"], manifest["decision_id"], manifest["order_intent_id"], manifest["client_order_id"], Decimal(str(manifest["approved_amount_usdt"])), manifest["order_payload_fingerprint"])
        actual = (self.canary_id, self.decision_id, self.order_intent_id, self.client_order_id, self.approved_amount_usdt, self.order_payload_fingerprint)
        if actual != expected or self.manifest_sha256 != manifest_sha256: raise LiveOrderSafetyError("approval does not bind exactly to the canary manifest")
        if self.exchange != "Bybit" or self.market_type != "spot" or self.symbol != "BTCUSDT" or self.side != "Buy" or self.order_type != "Market": raise LiveOrderSafetyError("approval market identity is not approved")


@dataclass(frozen=True)
class SpotMarketBuyRequest:
    qty: Decimal
    order_link_id: str

    def __post_init__(self) -> None:
        if self.qty <= 0 or self.qty != self.qty.to_integral_value(): raise LiveOrderSafetyError("Spot Market Buy quantity must be a positive whole-dollar quote amount")
        if not self.order_link_id: raise LiveOrderSafetyError("orderLinkId is required")

    def to_payload(self) -> dict[str, Any]:
        return {"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": format(self.qty.normalize(), "f"), "marketUnit": "quoteCoin", "isLeverage": 0, "orderLinkId": self.order_link_id, "orderFilter": "Order"}


@dataclass(frozen=True)
class OrderSubmissionAttempt:
    run_id: str
    canary_id: str
    approval_id: str
    manifest_sha256: str
    decision_id: str
    order_intent_id: str
    client_order_id: str
    approved_amount_usdt: Decimal
    request_fingerprint: str
    exchange: str
    market_type: str
    symbol: str
    side: str
    order_type: str
    created_at_utc: str
    state: str = "prepared"
    no_order_executed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "5.4.0", "run_id": self.run_id, "canary_id": self.canary_id, "approval_id": self.approval_id, "manifest_sha256": self.manifest_sha256, "decision_id": self.decision_id, "order_intent_id": self.order_intent_id, "client_order_id": self.client_order_id, "approved_amount_usdt": str(self.approved_amount_usdt), "request_fingerprint": self.request_fingerprint, "exchange": self.exchange, "market_type": self.market_type, "symbol": self.symbol, "side": self.side, "order_type": self.order_type, "created_at_utc": self.created_at_utc, "state": self.state, "no_order_executed": self.no_order_executed}


@dataclass(frozen=True)
class SubmissionOutcome:
    run_id: str
    decision_id: str
    order_intent_id: str
    client_order_id: str
    state: str
    completed_at_utc: str
    ret_code: int | None
    ret_msg: str | None
    order_id: str | None
    returned_order_link_id: str | None
    reconciliation_state: str
    ledger_not_mutated: bool = True
    outcome_category: str = "acknowledgement_received"

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "5.4.0", **self.__dict__}


@dataclass(frozen=True)
class ConfirmedFill:
    execution_id: str
    order_id: str
    order_link_id: str
    quantity_btc: Decimal
    quote_value_usdt: Decimal
    average_price_usdt: Decimal
    executed_at_utc: str
    fee: Decimal = Decimal("0")
    fee_asset: str = ""
    category: str = "spot"
    symbol: str = "BTCUSDT"


@dataclass(frozen=True)
class ReconciliationEvidence:
    state: str
    order_id: str | None = None
    fills: tuple[ConfirmedFill, ...] = ()
    order_link_id: str | None = None


@dataclass(frozen=True)
class SubmissionReconciliation:
    run_id: str
    decision_id: str
    order_intent_id: str
    canary_id: str
    approval_id: str
    client_order_id: str
    order_id: str
    reconciliation_state: str
    reconciled_at_utc: str
    fills: tuple[ConfirmedFill, ...]
    source: str = "Bybit private order/fill reconciliation"
    ledger_mutated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "5.4.1", "run_id": self.run_id, "decision_id": self.decision_id, "order_intent_id": self.order_intent_id, "canary_id": self.canary_id, "approval_id": self.approval_id, "client_order_id": self.client_order_id, "order_id": self.order_id, "reconciliation_state": self.reconciliation_state, "reconciled_at_utc": self.reconciled_at_utc, "fills": [{"exec_id": fill.execution_id, "order_id": fill.order_id, "order_link_id": fill.order_link_id, "category": fill.category, "symbol": fill.symbol, "quantity_btc": format(fill.quantity_btc, "f"), "quote_value_usdt": format(fill.quote_value_usdt, "f"), "average_price_usdt": format(fill.average_price_usdt, "f"), "executed_at_utc": fill.executed_at_utc, "fee": format(fill.fee, "f"), "fee_asset": fill.fee_asset} for fill in self.fills], "source": self.source, "ledger_mutated": self.ledger_mutated}


@dataclass(frozen=True)
class SubmissionResult:
    attempt: OrderSubmissionAttempt | None
    outcome: SubmissionOutcome


class SubmissionTransport(Protocol):
    def submit_spot_market_buy(self, request: SpotMarketBuyRequest) -> HttpResponse: ...


class PostAckReconciler(Protocol):
    """Fresh Phase 5.2 read-back performed after the POST response."""
    def reconcile_after_ack(self, client_order_id: str) -> ReconciliationEvidence: ...


class FreshBybitReadClient(Protocol):
    def credential_info(self) -> ApiCredentialInfo: ...
    def account_info(self): ...
    def wallet_balances(self): ...
    def spot_quote_availability(self): ...
    def instrument_rules(self): ...
    def quote_unit_limit_evidence(self): ...
    def submission_state(self, client_order_id: str) -> str: ...


@dataclass(frozen=True)
class _FreshInstrumentProvider:
    rules: Any
    def get_rules(self, exchange: str, market_type: str, symbol: str):
        if (exchange, market_type, symbol) != ("Bybit", "spot", "BTCUSDT"):
            raise LiveOrderSafetyError("fresh instrument identity is not approved")
        return self.rules


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class SignedBybitSubmissionTransport:
    """One-shot authenticated POST transport with no retry and no generic POST."""
    def __init__(self, api_key: str, api_secret: str, *, base_url: str = "https://api.bybit.com", clock: Callable[[], int] | None = None, timeout_seconds: float = 10.0) -> None:
        if not api_key or not api_secret: raise LiveOrderSafetyError("Bybit submission credentials are unavailable")
        from urllib.parse import urlsplit
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname: raise LiveOrderSafetyError("Bybit submission base URL must be HTTPS")
        self._host, self._port, self._base_path = parsed.hostname, parsed.port, parsed.path.rstrip("/")
        self._api_key, self._secret = api_key, api_secret
        self._clock, self._timeout = clock or (lambda: int(time.time() * 1000)), timeout_seconds

    def submit_spot_market_buy(self, request: SpotMarketBuyRequest) -> HttpResponse:
        body = _canonical_json(request.to_payload())
        timestamp = str(self._clock())
        signature_payload = f"{timestamp}{self._api_key}{RECV_WINDOW}{body}"
        sign = hmac.new(self._secret.encode(), signature_payload.encode(), hashlib.sha256).hexdigest()
        headers = {"Accept": "application/json", "Content-Type": "application/json", "X-BAPI-API-KEY": self._api_key, "X-BAPI-SIGN-TYPE": "2", "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": RECV_WINDOW, "X-BAPI-SIGN": sign}
        import http.client
        conn = http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout)
        try:
            conn.request("POST", f"{self._base_path}{ORDER_CREATE_PATH}", body=body.encode(), headers=headers)
            raw = conn.getresponse()
            return HttpResponse(raw.status, {k.lower(): v for k, v in raw.getheaders()}, raw.read())
        except (socket.timeout, TimeoutError, OSError) as exc:
            raise AmbiguousSubmissionError("Bybit order submission outcome is ambiguous; reconcile before retry") from exc
        finally: conn.close()


class LiveOrderEngine:
    def __init__(self, *, artifact_store: ArtifactStore, now: Callable[[], datetime] | None = None, availability_policy: availability.SpotQuoteAvailabilityPolicy = availability.PRODUCTION_AVAILABILITY_POLICY, quote_limit_policy: QuoteUnitLimitPolicy = PRODUCTION_QUOTE_UNIT_LIMIT_POLICY) -> None:
        self.artifact_store = artifact_store
        self._now = now or (lambda: datetime.now(UTC))
        self.availability_policy = availability_policy
        self.quote_limit_policy = quote_limit_policy

    @staticmethod
    def _fingerprint(request: SpotMarketBuyRequest) -> str:
        return hashlib.sha256(_canonical_json(request.to_payload()).encode()).hexdigest()

    def _outcome(self, intent: OrderIntent, run_id: str, state: str, *, code: int | None = None, msg: str | None = None, order_id: str | None = None, returned_link: str | None = None, reconciliation: str = "not_applicable", category: str | None = None) -> SubmissionOutcome:
        if category is None: category = {"blocked": "request_not_sent", "ambiguous": "reconciliation_required", "rejected_by_exchange": "exchange_rejected", "acknowledged": "acknowledgement_received"}.get(state, "reconciliation_required")
        return SubmissionOutcome(run_id, intent.decision_id, intent.order_intent_id, intent.client_order_id, state, self._now().astimezone(UTC).isoformat().replace("+00:00", "Z"), code, msg, order_id, returned_link, reconciliation, True, category)

    def submit(self, intent: OrderIntent, decision: Mapping[str, Any], *, calendar_month: str, ledger_path, execution_config: ExecutionConfig, approval: LiveApproval, approval_sha256: str, manifest: Mapping[str, Any], manifest_sha256: str, read_client: FreshBybitReadClient, transport: SubmissionTransport, run_id: str, post_ack_reconciler: PostAckReconciler) -> SubmissionResult:
        if not execution_config.live_execution_enabled: return SubmissionResult(None, self._outcome(intent, run_id, "blocked", msg="LIVE EXECUTION DISABLED"))
        if execution_config.kill_switch: return SubmissionResult(None, self._outcome(intent, run_id, "blocked", msg="KILL SWITCH ACTIVE"))
        if execution_config.order_submission not in {"implemented_disabled", "implemented"}: return SubmissionResult(None, self._outcome(intent, run_id, "blocked", msg="ORDER SUBMISSION MODE DISABLED"))
        if manifest is None: return SubmissionResult(None, self._outcome(intent, run_id, "blocked", msg="CANARY MANIFEST REQUIRED"))
        if hasattr(manifest, "to_dict"):
            manifest = manifest.to_dict()
        if approval is None or not approval_sha256: return SubmissionResult(None, self._outcome(intent, run_id, "blocked", msg="PERSISTED LIVE APPROVAL REQUIRED"))
        if read_client is None: return SubmissionResult(None, self._outcome(intent, run_id, "blocked", msg="FRESH PRIVATE READ CLIENT REQUIRED"))
        if post_ack_reconciler is None: return SubmissionResult(None, self._outcome(intent, run_id, "blocked", msg="POST-ACK RECONCILER REQUIRED"))
        self._validate_persisted_manifest(manifest, manifest_sha256)
        self._validate_manifest(manifest, intent, execution_config, manifest_sha256=manifest_sha256, now_utc=self._now().astimezone(UTC))
        self._validate_persisted_approval(approval, approval_sha256, intent, manifest, manifest_sha256, now_utc=self._now().astimezone(UTC))
        credential_info, instrument_provider, submission_state = self._fresh_private_checks(read_client, intent, availability_policy=self.availability_policy, quote_limit_policy=self.quote_limit_policy, now_utc=self._now().astimezone(UTC))
        from .ledger import confirmed_executions, read_executions
        try:
            for execution in confirmed_executions(read_executions(ledger_path)):
                if execution.payload.get("decision_id") == intent.decision_id or execution.payload.get("client_order_id") == intent.client_order_id:
                    raise LiveOrderSafetyError("prior confirmed execution requires reconciliation")
        except LiveOrderSafetyError: raise
        except Exception as exc: raise LiveOrderSafetyError("canonical ledger reread failed") from exc
        from .execution import validate_execution_safety
        safety = validate_execution_safety(intent, decision, ledger_path=ledger_path, calendar_month=calendar_month, execution_config=execution_config, instrument_provider=instrument_provider, submission_state=submission_state, live_approval_granted=True)
        if safety.status != "approved": raise LiveOrderSafetyError("pre-submission safety validation failed: " + "; ".join(safety.reasons))
        request = SpotMarketBuyRequest(intent.quote_amount_usdt, intent.client_order_id)
        attempt = OrderSubmissionAttempt(run_id, manifest["canary_id"], approval.approval_id, manifest_sha256, intent.decision_id, intent.order_intent_id, intent.client_order_id, intent.quote_amount_usdt, self._fingerprint(request), "Bybit", "spot", "BTCUSDT", "Buy", "Market", self._now().astimezone(UTC).isoformat().replace("+00:00", "Z"))
        if self.artifact_store.has_submission_attempt_identity(approval_id=approval.approval_id, canary_id=manifest["canary_id"], decision_id=intent.decision_id, client_order_id=intent.client_order_id): raise LiveOrderSafetyError("prior prepared or ambiguous submission requires reconciliation")
        try: self.artifact_store.persist(ArtifactType.ORDER_SUBMISSION_ATTEMPT, attempt, run_id=run_id)
        except ArtifactAlreadyExistsError as exc: raise LiveOrderSafetyError("submission attempt already exists; reconciliation required") from exc
        try:
            response = transport.submit_spot_market_buy(request)
            outcome, evidence = self._parse_response(intent, run_id, response, post_ack_reconciler, canary_id=manifest["canary_id"], approval_id=approval.approval_id)
        except AmbiguousSubmissionError:
            # The transport boundary may have been crossed. Reconcile before
            # returning control, and never retry this approval/attempt.
            outcome, evidence = self._reconcile_after_possible_submission(intent, run_id, post_ack_reconciler, canary_id=manifest["canary_id"], approval_id=approval.approval_id)
        if evidence is not None and evidence.state == "confirmed" and evidence.fills:
            self._append_aggregate_execution(ledger_path, evidence, intent, manifest["canary_id"], approval.approval_id)
            outcome = SubmissionOutcome(outcome.run_id, outcome.decision_id, outcome.order_intent_id, outcome.client_order_id, outcome.state, outcome.completed_at_utc, outcome.ret_code, outcome.ret_msg, outcome.order_id, outcome.returned_order_link_id, "confirmed", False, "confirmed_execution")
        self.artifact_store.persist(ArtifactType.ORDER_SUBMISSION_OUTCOME, outcome, run_id=run_id)
        return SubmissionResult(attempt, outcome)

    @staticmethod
    def _validate_manifest(manifest: Mapping[str, Any], intent: OrderIntent, config: ExecutionConfig, *, manifest_sha256: str, now_utc: datetime) -> None:
        try:
            validate_artifact("canary_manifest", dict(manifest))
        except Exception as exc:
            raise LiveOrderSafetyError(f"canary manifest schema validation failed: {exc}") from exc
        active_strategy = load_strategy_config()
        required = {"canary_status": "READY_FOR_MANUAL_APPROVAL", "strategy_id": active_strategy.strategy_id, "strategy_version": active_strategy.strategy_version, "exchange": "Bybit", "market_type": "spot", "symbol": "BTCUSDT", "side": "Buy", "order_type": "Market", "decision_id": intent.decision_id, "order_intent_id": intent.order_intent_id, "client_order_id": intent.client_order_id}
        if any(manifest.get(k) != v for k, v in required.items()): raise LiveOrderSafetyError("canary manifest identity or status is invalid")
        if manifest.get("live_execution_enabled") is not False or manifest.get("kill_switch") is not True: raise LiveOrderSafetyError("manifest production guards do not match approved execution mode")
        try:
            prepared = _utc(str(manifest["prepared_at_utc"]))
            expires = _utc(str(manifest["expires_at_utc"]))
            if prepared > now_utc or not prepared <= now_utc < expires: raise LiveOrderSafetyError("canary manifest is future-dated or expired")
            if expires - prepared > timedelta(minutes=15): raise LiveOrderSafetyError("canary manifest TTL exceeds 15 minutes")
        except KeyError as exc: raise LiveOrderSafetyError("canary manifest timestamp is missing") from exc
        if config.live_execution_enabled is not True or config.kill_switch is not False: raise LiveOrderSafetyError("live execution requires explicit non-production test configuration; checked-in production defaults remain blocked")
        request = SpotMarketBuyRequest(intent.quote_amount_usdt, intent.client_order_id)
        expected = hashlib.sha256(_canonical_json(request.to_payload()).encode()).hexdigest()
        if manifest.get("order_payload_fingerprint") != expected or manifest.get("approved_amount_usdt") != str(intent.quote_amount_usdt): raise LiveOrderSafetyError("manifest payload or amount does not match freshly reconstructed request")
        actual_manifest_sha256 = hashlib.sha256((_canonical_json(manifest) + "\n").encode()).hexdigest()
        if manifest_sha256 != actual_manifest_sha256: raise LiveOrderSafetyError("manifest SHA-256 does not match immutable manifest bytes")

    def _validate_persisted_manifest(self, manifest: Mapping[str, Any], manifest_sha256: str) -> None:
        try:
            persisted, persisted_sha = self.artifact_store.find_artifact(ArtifactType.CANARY_MANIFEST, identity_field="canary_id", identity_value=str(manifest["canary_id"]))
        except Exception as exc:
            raise LiveOrderSafetyError("persisted CanaryManifest is required") from exc
        if persisted_sha != manifest_sha256 or persisted != dict(manifest):
            raise LiveOrderSafetyError("persisted CanaryManifest digest or bytes do not match supplied manifest")

    def _validate_persisted_approval(self, approval: LiveApproval, approval_sha256: str, intent: OrderIntent, manifest: Mapping[str, Any], manifest_sha256: str, *, now_utc: datetime) -> None:
        self._validate_persisted_approval_receipt(approval, approval_sha256)
        approval.validate(intent, now_utc=now_utc, manifest=manifest, manifest_sha256=manifest_sha256)

    def _validate_persisted_approval_receipt(self, approval: LiveApproval, approval_sha256: str) -> None:
        try:
            persisted, persisted_sha = self.artifact_store.find_artifact(ArtifactType.LIVE_APPROVAL, identity_field="approval_id", identity_value=approval.approval_id)
        except Exception as exc:
            raise LiveOrderSafetyError("persisted LiveApproval is required") from exc
        if persisted_sha != approval_sha256 or persisted != approval.to_dict(): raise LiveOrderSafetyError("persisted LiveApproval digest or bytes do not match supplied approval")
        try:
            validate_live_approval(persisted)
        except Exception as exc:
            raise LiveOrderSafetyError("persisted LiveApproval schema validation failed") from exc

    @staticmethod
    def _fresh_private_checks(client: FreshBybitReadClient, intent: OrderIntent, *, availability_policy: availability.SpotQuoteAvailabilityPolicy = availability.PRODUCTION_AVAILABILITY_POLICY, quote_limit_policy: QuoteUnitLimitPolicy = PRODUCTION_QUOTE_UNIT_LIMIT_POLICY, now_utc: datetime | None = None) -> tuple[ApiCredentialInfo, Any, SubmissionState]:
        info = client.credential_info()
        if info.classification is not CredentialClassification.TRADE_CAPABLE: raise LiveOrderSafetyError("fresh credential classification is not TRADE_CAPABLE")
        if set(info.permissions) - {"Spot", "Wallet"} or not any(action in {"SpotTrade", "OrderEntry", "SpotOrder"} for action in info.permissions.get("Spot", ())): raise LiveOrderSafetyError("fresh credential permissions are not Spot-only approved trade scope")
        account = client.account_info()
        if not all((account.unified_margin_status, account.margin_mode, account.spot_hedging_status, account.updated_time)): raise LiveOrderSafetyError("fresh account context is incomplete")
        balances = {row.coin: row for row in client.wallet_balances()}
        if "BTC" not in balances or "USDT" not in balances: raise LiveOrderSafetyError("fresh BTC/USDT wallet evidence is incomplete")
        if any(row.has_liability for row in balances.values()): raise LiveOrderSafetyError("fresh wallet liability detected")
        try:
            source = client.spot_quote_availability() if hasattr(client, "spot_quote_availability") else None
            available = availability.validate_spot_quote_availability(source, now=(now_utc or datetime.now(UTC)), policy=availability_policy)
        except availability.AvailabilityValidationError as exc:
            raise LiveOrderSafetyError("fresh authoritative exact Spot quote-buy availability is unavailable or untrusted") from exc
        if available < intent.quote_amount_usdt: raise LiveOrderSafetyError("fresh authoritative exact Spot quote-buy availability is unavailable or insufficient")
        if (intent.exchange, intent.market_type, intent.symbol, intent.side) != ("Bybit", "spot", "BTCUSDT", "Buy"):
            raise LiveOrderSafetyError("live request must be the approved Bybit Spot BTCUSDT Market Buy")
        if intent.quote_amount_usdt <= 0:
            raise LiveOrderSafetyError("quote-denominated order amount must be positive")
        # marketUnit=quoteCoin makes qty the exact approved USDT amount. No
        # base quantity, ticker price, wallet balance, or deprecated maxOrderAmt
        # is used to invent a quote ceiling.
        rules = client.instrument_rules()
        rules.validate_quote(intent.quote_amount_usdt)
        if client.submission_state(intent.client_order_id) != "conclusively_absent": raise LiveOrderSafetyError("fresh order reconciliation is not conclusively absent")
        return info, _FreshInstrumentProvider(rules), BybitSubmissionEvidence(client, intent.client_order_id)

    @staticmethod
    def _validate_fill(fill: ConfirmedFill, authoritative_order_id: str | None, client_order_id: str) -> None:
        if fill.category != "spot" or fill.symbol != "BTCUSDT" or fill.order_link_id != client_order_id or not authoritative_order_id or fill.order_id != authoritative_order_id: raise LiveOrderSafetyError("fill identity is contradictory")
        if not fill.execution_id or not fill.fee_asset or fill.quantity_btc <= 0 or fill.quote_value_usdt <= 0 or fill.average_price_usdt <= 0: raise LiveOrderSafetyError("fill values or authoritative fee identity are malformed")
        _utc(fill.executed_at_utc)

    def _persist_reconciliation(self, intent: OrderIntent, run_id: str, canary_id: str, approval_id: str, evidence: ReconciliationEvidence) -> None:
        if not evidence.order_id:
            raise LiveOrderSafetyError("reconciliation lacks authoritative order ID")
        artifact = SubmissionReconciliation(run_id, intent.decision_id, intent.order_intent_id, canary_id, approval_id, intent.client_order_id, evidence.order_id, evidence.state, self._now().astimezone(UTC).isoformat().replace("+00:00", "Z"), evidence.fills)
        digest = hashlib.sha256(_canonical_json(artifact.to_dict()).encode()).hexdigest()
        artifact_run_id = f"{run_id}_recon_{digest[:16]}"
        try:
            self.artifact_store.persist(ArtifactType.SUBMISSION_RECONCILIATION, artifact, run_id=artifact_run_id)
        except ArtifactAlreadyExistsError:
            # A byte-identical observation is idempotent. Read-back verifies
            # the immutable canonical bytes and digest before accepting it.
            prior = self.artifact_store.submission_reconciliations(decision_id=intent.decision_id, canary_id=canary_id, client_order_id=intent.client_order_id, order_id=evidence.order_id)
            if not any(item == artifact.to_dict() for item in prior):
                raise LiveOrderSafetyError("reconciliation artifact identity conflicts")

    @staticmethod
    def _fill_from_artifact(payload: Mapping[str, Any]) -> ConfirmedFill:
        try:
            return ConfirmedFill(str(payload["exec_id"]), str(payload["order_id"]), str(payload["order_link_id"]), Decimal(str(payload["quantity_btc"])), Decimal(str(payload["quote_value_usdt"])), Decimal(str(payload["average_price_usdt"])), str(payload["executed_at_utc"]), Decimal(str(payload["fee"])), str(payload["fee_asset"]), str(payload["category"]), str(payload["symbol"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise LiveOrderSafetyError("persisted reconciliation fill is malformed") from exc

    def _combined_reconciliation_fills(self, intent: OrderIntent, canary_id: str, approval_id: str, current: ReconciliationEvidence) -> tuple[ConfirmedFill, ...]:
        if not current.order_id:
            raise LiveOrderSafetyError("confirmed reconciliation lacks order ID")
        snapshots = self.artifact_store.submission_reconciliations(decision_id=intent.decision_id, canary_id=canary_id, client_order_id=intent.client_order_id, order_id=current.order_id)
        fills: dict[str, ConfirmedFill] = {}
        for snapshot in snapshots:
            if snapshot.get("order_intent_id") != intent.order_intent_id or snapshot.get("approval_id") != approval_id:
                raise LiveOrderSafetyError("persisted reconciliation identity conflicts")
            for raw_fill in snapshot["fills"]:
                fill = self._fill_from_artifact(raw_fill)
                self._validate_fill(fill, current.order_id, intent.client_order_id)
                existing = fills.get(fill.execution_id)
                if existing is not None and existing != fill:
                    raise LiveOrderSafetyError("contradictory reconciliation evidence for execution ID")
                fills[fill.execution_id] = fill
        if not fills:
            raise LiveOrderSafetyError("confirmed reconciliation has no authoritative fills")
        return tuple(fills[execution_id] for execution_id in sorted(fills))

    def _append_aggregate_execution(self, ledger_path, evidence: ReconciliationEvidence, intent: OrderIntent, canary_id: str, approval_id: str) -> None:
        if not evidence.order_id or not evidence.fills:
            raise LiveOrderSafetyError("cannot append an unconfirmed execution")
        for fill in evidence.fills:
            self._validate_fill(fill, evidence.order_id, intent.client_order_id)
        fee_assets = {fill.fee_asset for fill in evidence.fills}
        if len(fee_assets) != 1:
            raise LiveOrderSafetyError("multiple authoritative fee assets require reconciliation")
        total_btc = sum((fill.quantity_btc for fill in evidence.fills), Decimal("0"))
        total_quote = sum((fill.quote_value_usdt for fill in evidence.fills), Decimal("0"))
        total_fee = sum((fill.fee for fill in evidence.fills), Decimal("0"))
        if total_btc <= 0 or total_quote <= 0:
            raise LiveOrderSafetyError("aggregate fill values are malformed")
        execution_ids = tuple(sorted(fill.execution_id for fill in evidence.fills))
        aggregate_id = hashlib.sha256((evidence.order_id + "|" + intent.client_order_id + "|" + "|".join(execution_ids)).encode()).hexdigest()[:32]
        completed_at = max((_utc(fill.executed_at_utc) for fill in evidence.fills)).isoformat().replace("+00:00", "Z")
        payload = {"schema_version": "1.1.0", "execution_id": "execution_order_" + aggregate_id, "executed_at_utc": completed_at, "asset": "BTC", "quote_currency": "USDT", "executed_usd": float(total_quote), "reference_price_usdt": float(total_quote / total_btc), "btc_quantity": float(total_btc), "status": "reconciled", "reconciliation": {"source": "Bybit private order/fill reconciliation", "note": "authoritative aggregated Spot BTCUSDT fills bound to orderLinkId"}, "decision_id": intent.decision_id, "canary_id": canary_id, "approval_id": approval_id, "order_id": evidence.order_id, "order_link_id": intent.client_order_id, "execution_id_bybit": ",".join(execution_ids), "fee": float(total_fee), "fee_asset": fee_assets.pop()}
        append_execution_once(ledger_path, payload)

    def reconcile_existing(self, intent: OrderIntent, *, ledger_path, approval: LiveApproval, approval_sha256: str, manifest: Mapping[str, Any], manifest_sha256: str, run_id: str, post_ack_reconciler: PostAckReconciler) -> SubmissionResult:
        """Read-only recovery path for a prior attempt; it cannot submit an order."""
        if manifest is None or approval is None or post_ack_reconciler is None:
            raise LiveOrderSafetyError("persisted manifest, approval, and fresh reconciler are required")
        self._validate_persisted_manifest(manifest, manifest_sha256)
        self._validate_persisted_approval_receipt(approval, approval_sha256)
        outcome, evidence = self._reconcile_after_possible_submission(intent, run_id, post_ack_reconciler, canary_id=str(manifest["canary_id"]), approval_id=approval.approval_id)
        if evidence is not None and evidence.state == "confirmed" and evidence.fills:
            self._append_aggregate_execution(ledger_path, evidence, intent, str(manifest["canary_id"]), approval.approval_id)
            outcome = SubmissionOutcome(outcome.run_id, outcome.decision_id, outcome.order_intent_id, outcome.client_order_id, outcome.state, outcome.completed_at_utc, outcome.ret_code, outcome.ret_msg, outcome.order_id, outcome.returned_order_link_id, "confirmed", False, "confirmed_execution")
        self.artifact_store.persist(ArtifactType.ORDER_SUBMISSION_OUTCOME, outcome, run_id=run_id)
        return SubmissionResult(None, outcome)

    def _reconcile_after_possible_submission(self, intent: OrderIntent, run_id: str, reconciler: PostAckReconciler, *, canary_id: str, approval_id: str, ack_order_id: str | None = None, ack_order_link_id: str | None = None) -> tuple[SubmissionOutcome, ReconciliationEvidence | None]:
        try:
            evidence = reconciler.reconcile_after_ack(intent.client_order_id)
            if not isinstance(evidence, ReconciliationEvidence):
                raise LiveOrderSafetyError("reconciler returned malformed evidence")
            if evidence.order_link_id is not None and evidence.order_link_id != intent.client_order_id:
                raise LiveOrderSafetyError("reconciled orderLinkId is contradictory")
            if ack_order_link_id is not None and ack_order_link_id != intent.client_order_id:
                raise LiveOrderSafetyError("ACK orderLinkId is contradictory")
            if ack_order_id is not None and evidence.order_id != ack_order_id:
                raise LiveOrderSafetyError("ACK orderId differs from reconciled orderId")
            fill_ids = [fill.execution_id for fill in evidence.fills]
            if len(fill_ids) != len(set(fill_ids)):
                raise LiveOrderSafetyError("duplicate execution identity in reconciliation")
            for fill in evidence.fills:
                self._validate_fill(fill, evidence.order_id, intent.client_order_id)
            if evidence.state in {"active", "partial", "confirmed"}:
                if not evidence.order_id:
                    raise LiveOrderSafetyError("reconciliation lacks authoritative order ID")
                if evidence.state == "active" and evidence.fills:
                    raise LiveOrderSafetyError("active order unexpectedly has fill evidence")
                if evidence.state in {"partial", "confirmed"} and not evidence.fills:
                    raise LiveOrderSafetyError("filled reconciliation lacks authoritative fills")
                self._persist_reconciliation(intent, run_id, canary_id, approval_id, evidence)
            if evidence.state == "confirmed":
                if not evidence.order_id or not evidence.fills:
                    raise LiveOrderSafetyError("confirmed reconciliation lacks authoritative fills")
                combined = self._combined_reconciliation_fills(intent, canary_id, approval_id, evidence)
                return self._outcome(intent, run_id, "acknowledged", order_id=evidence.order_id, returned_link=intent.client_order_id, reconciliation="confirmed", category="confirmed_execution"), ReconciliationEvidence("confirmed", evidence.order_id, combined, intent.client_order_id)
            if evidence.state in {"active", "partial", "ambiguous", "conclusively_absent"}:
                schema_state = evidence.state if evidence.state in {"partial", "ambiguous", "conclusively_absent"} else "ambiguous"
                return self._outcome(intent, run_id, "ambiguous", msg=f"reconciliation state: {evidence.state}", order_id=evidence.order_id, returned_link=intent.client_order_id, reconciliation=schema_state, category="reconciliation_required"), evidence
            raise LiveOrderSafetyError("reconciler returned unknown state")
        except Exception as exc:
            return self._outcome(intent, run_id, "ambiguous", msg=f"reconciliation required: {exc}", returned_link=intent.client_order_id, reconciliation="ambiguous", category="reconciliation_required"), None

    def _parse_response(self, intent: OrderIntent, run_id: str, response: HttpResponse, post_ack_reconciler: PostAckReconciler, *, canary_id: str, approval_id: str) -> tuple[SubmissionOutcome, ReconciliationEvidence | None]:
        if response.status >= 500:
            return self._reconcile_after_possible_submission(intent, run_id, post_ack_reconciler, canary_id=canary_id, approval_id=approval_id)
        if response.status >= 400: return self._outcome(intent, run_id, "rejected_by_exchange", msg="HTTP response rejected submission", category="exchange_rejected"), None
        try: body = json.loads(response.body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError): return self._reconcile_after_possible_submission(intent, run_id, post_ack_reconciler, canary_id=canary_id, approval_id=approval_id)
        if not isinstance(body, dict): return self._reconcile_after_possible_submission(intent, run_id, post_ack_reconciler, canary_id=canary_id, approval_id=approval_id)
        code, msg, result = body.get("retCode"), body.get("retMsg"), body.get("result")
        if code != 0:
            if not isinstance(code, int): return self._reconcile_after_possible_submission(intent, run_id, post_ack_reconciler, canary_id=canary_id, approval_id=approval_id)
            return self._outcome(intent, run_id, "rejected_by_exchange", code=code, msg=str(msg) if msg is not None else None, category="exchange_rejected"), None
        if not isinstance(result, dict): return self._reconcile_after_possible_submission(intent, run_id, post_ack_reconciler, canary_id=canary_id, approval_id=approval_id)
        ack_order_id = result.get("orderId") if isinstance(result.get("orderId"), str) else None
        ack_order_link_id = result.get("orderLinkId") if isinstance(result.get("orderLinkId"), str) else None
        if ack_order_id is None or ack_order_link_id is None:
            return self._reconcile_after_possible_submission(intent, run_id, post_ack_reconciler, canary_id=canary_id, approval_id=approval_id, ack_order_id=ack_order_id, ack_order_link_id=ack_order_link_id)
        return self._reconcile_after_possible_submission(intent, run_id, post_ack_reconciler, canary_id=canary_id, approval_id=approval_id, ack_order_id=ack_order_id, ack_order_link_id=ack_order_link_id)
