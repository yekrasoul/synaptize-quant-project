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
from .config import ExecutionConfig
from .errors import ArtifactAlreadyExistsError
from .execution import OrderIntent, SubmissionState, validate_execution_safety
from .ledger import append_execution_once
from .market_data.http import HttpResponse
from .private_bybit import ApiCredentialInfo, CredentialClassification

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
    approval_id: str | None = None
    canary_id: str | None = None
    manifest_sha256: str | None = None
    order_payload_fingerprint: str | None = None
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

    def validate(self, intent: OrderIntent, *, now_utc: datetime, manifest: Mapping[str, Any] | None = None, manifest_sha256: str | None = None) -> None:
        if (self.decision_id, self.order_intent_id, self.client_order_id) != (intent.decision_id, intent.order_intent_id, intent.client_order_id):
            raise LiveOrderSafetyError("explicit live approval does not bind to the immutable OrderIntent")
        if self.approved_amount_usdt != intent.quote_amount_usdt: raise LiveOrderSafetyError("explicit live approval amount does not match OrderIntent")
        approved, expires = _utc(self.approved_at_utc), _utc(self.expires_at_utc)
        if expires <= approved or now_utc < approved or now_utc >= expires: raise LiveOrderSafetyError("explicit live approval is expired or not yet valid")
        if self.standing_authorization: raise LiveOrderSafetyError("standing authorization is prohibited")
        if manifest is not None:
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
    decision_id: str
    order_intent_id: str
    client_order_id: str
    approved_amount_usdt: Decimal
    request_fingerprint: str
    created_at_utc: str
    state: str = "prepared"
    no_order_executed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "5.3.0", "run_id": self.run_id, "decision_id": self.decision_id, "order_intent_id": self.order_intent_id, "client_order_id": self.client_order_id, "approved_amount_usdt": str(self.approved_amount_usdt), "request_fingerprint": self.request_fingerprint, "created_at_utc": self.created_at_utc, "state": self.state, "no_order_executed": self.no_order_executed}


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
        return {"schema_version": "5.3.0", **self.__dict__}


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


@dataclass(frozen=True)
class ReconciliationEvidence:
    state: str
    order_id: str | None = None
    fills: tuple[ConfirmedFill, ...] = ()


@dataclass(frozen=True)
class SubmissionResult:
    attempt: OrderSubmissionAttempt | None
    outcome: SubmissionOutcome


class SubmissionTransport(Protocol):
    def submit_spot_market_buy(self, request: SpotMarketBuyRequest) -> HttpResponse: ...


class PostAckReconciler(Protocol):
    """Fresh Phase 5.2 read-back performed after the POST response."""
    def reconcile_after_ack(self, client_order_id: str) -> str: ...


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
    def __init__(self, *, artifact_store: ArtifactStore, now: Callable[[], datetime] | None = None) -> None:
        self.artifact_store = artifact_store
        self._now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def _fingerprint(request: SpotMarketBuyRequest) -> str:
        return hashlib.sha256(_canonical_json(request.to_payload()).encode()).hexdigest()

    def _outcome(self, intent: OrderIntent, run_id: str, state: str, *, code: int | None = None, msg: str | None = None, order_id: str | None = None, returned_link: str | None = None, reconciliation: str = "not_applicable", category: str | None = None) -> SubmissionOutcome:
        if category is None: category = {"blocked": "request_not_sent", "ambiguous": "reconciliation_required", "rejected_by_exchange": "exchange_rejected", "acknowledged": "acknowledgement_received"}.get(state, "reconciliation_required")
        return SubmissionOutcome(run_id, intent.decision_id, intent.order_intent_id, intent.client_order_id, state, self._now().astimezone(UTC).isoformat().replace("+00:00", "Z"), code, msg, order_id, returned_link, reconciliation, True, category)

    def submit(self, intent: OrderIntent, decision: Mapping[str, Any], *, calendar_month: str, ledger_path, execution_config: ExecutionConfig, approval: LiveApproval | None, credential_info: ApiCredentialInfo, instrument_provider, submission_state: SubmissionState | None, transport: SubmissionTransport, run_id: str, post_ack_reconciler: PostAckReconciler | None = None, manifest: Mapping[str, Any] | None = None, manifest_sha256: str | None = None, read_client: Any | None = None) -> SubmissionResult:
        if not execution_config.live_execution_enabled: return SubmissionResult(None, self._outcome(intent, run_id, "blocked", msg="LIVE EXECUTION DISABLED"))
        if execution_config.kill_switch: return SubmissionResult(None, self._outcome(intent, run_id, "blocked", msg="KILL SWITCH ACTIVE"))
        if execution_config.order_submission not in {"implemented_disabled", "implemented"}: return SubmissionResult(None, self._outcome(intent, run_id, "blocked", msg="ORDER SUBMISSION MODE DISABLED"))
        if approval is None: return SubmissionResult(None, self._outcome(intent, run_id, "blocked", msg="EXPLICIT LIVE APPROVAL REQUIRED"))
        if manifest is not None:
            self._validate_manifest(manifest, intent, execution_config, manifest_sha256=manifest_sha256)
        approval.validate(intent, now_utc=self._now().astimezone(UTC), manifest=manifest, manifest_sha256=manifest_sha256)
        if credential_info.classification is not CredentialClassification.TRADE_CAPABLE or not any(action in {"SpotTrade", "OrderEntry", "SpotOrder"} for action in credential_info.permissions.get("Spot", ())): raise LiveOrderSafetyError("credential is not approved for Spot trade capability")
        if read_client is not None:
            self._fresh_private_checks(read_client, intent)
        if submission_state is None: raise LiveOrderSafetyError("submission evidence unavailable")
        if submission_state.client_order_state(intent.client_order_id) != "conclusively_absent": raise LiveOrderSafetyError("REJECT NEW SUBMISSION: reconciliation required")
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
        attempt = OrderSubmissionAttempt(run_id, intent.decision_id, intent.order_intent_id, intent.client_order_id, intent.quote_amount_usdt, self._fingerprint(request), self._now().astimezone(UTC).isoformat().replace("+00:00", "Z"))
        if self.artifact_store.has_submission_attempt(intent.decision_id, intent.client_order_id): raise LiveOrderSafetyError("prior prepared or ambiguous submission requires reconciliation")
        try: self.artifact_store.persist(ArtifactType.ORDER_SUBMISSION_ATTEMPT, attempt, run_id=run_id)
        except ArtifactAlreadyExistsError as exc: raise LiveOrderSafetyError("submission attempt already exists; reconciliation required") from exc
        try:
            response = transport.submit_spot_market_buy(request)
        except AmbiguousSubmissionError:
            outcome = self._outcome(intent, run_id, "ambiguous", msg="submission outcome is ambiguous", category="transport_ambiguous")
            self.artifact_store.persist(ArtifactType.ORDER_SUBMISSION_OUTCOME, outcome, run_id=run_id)
            raise
        outcome, evidence = self._parse_response(intent, run_id, response, post_ack_reconciler)
        if evidence is not None and evidence.state == "confirmed" and evidence.fills:
            for fill in evidence.fills:
                payload = {"schema_version": "1.0.0", "execution_id": "execution_" + fill.execution_id, "executed_at_utc": fill.executed_at_utc, "asset": "BTC", "quote_currency": "USDT", "executed_usd": float(fill.quote_value_usdt), "reference_price_usdt": float(fill.average_price_usdt), "btc_quantity": float(fill.quantity_btc), "status": "reconciled", "reconciliation": {"source": "Bybit private order/fill reconciliation", "note": "authoritative Spot BTCUSDT fill bound to orderLinkId"}, "decision_id": intent.decision_id, "order_id": fill.order_id, "order_link_id": fill.order_link_id, "execution_id_bybit": fill.execution_id, "fee": float(fill.fee), "fee_asset": fill.fee_asset}
                if manifest: payload["canary_id"] = manifest["canary_id"]
                if approval.approval_id: payload["approval_id"] = approval.approval_id
                append_execution_once(ledger_path, payload)
            outcome = SubmissionOutcome(outcome.run_id, outcome.decision_id, outcome.order_intent_id, outcome.client_order_id, outcome.state, outcome.completed_at_utc, outcome.ret_code, outcome.ret_msg, outcome.order_id, outcome.returned_order_link_id, "confirmed", False, "confirmed_execution")
        self.artifact_store.persist(ArtifactType.ORDER_SUBMISSION_OUTCOME, outcome, run_id=run_id)
        return SubmissionResult(attempt, outcome)

    @staticmethod
    def _validate_manifest(manifest: Mapping[str, Any], intent: OrderIntent, config: ExecutionConfig, *, manifest_sha256: str | None) -> None:
        required = {"canary_status": "READY_FOR_MANUAL_APPROVAL", "strategy_id": "btc_adaptive_dca_v1", "strategy_version": "1.0.0", "exchange": "Bybit", "market_type": "spot", "symbol": "BTCUSDT", "side": "Buy", "order_type": "Market", "decision_id": intent.decision_id, "order_intent_id": intent.order_intent_id, "client_order_id": intent.client_order_id}
        if any(manifest.get(k) != v for k, v in required.items()): raise LiveOrderSafetyError("canary manifest identity or status is invalid")
        if manifest.get("live_execution_enabled") is not False or manifest.get("kill_switch") is not True: raise LiveOrderSafetyError("manifest production guards do not match approved execution mode")
        try:
            prepared = _utc(str(manifest["prepared_at_utc"]))
            if datetime.now(UTC) - prepared > timedelta(minutes=15): raise LiveOrderSafetyError("canary manifest is expired or older than 15 minutes")
        except KeyError as exc: raise LiveOrderSafetyError("canary manifest timestamp is missing") from exc
        if config.live_execution_enabled is not True or config.kill_switch is not False: raise LiveOrderSafetyError("live execution requires explicit non-production test configuration; checked-in production defaults remain blocked")
        request = SpotMarketBuyRequest(intent.quote_amount_usdt, intent.client_order_id)
        expected = hashlib.sha256(_canonical_json(request.to_payload()).encode()).hexdigest()
        if manifest.get("order_payload_fingerprint") != expected or manifest.get("approved_amount_usdt") != str(intent.quote_amount_usdt): raise LiveOrderSafetyError("manifest payload or amount does not match freshly reconstructed request")
        if manifest_sha256 is None: raise LiveOrderSafetyError("manifest SHA-256 is required")
        actual_manifest_sha256 = hashlib.sha256((_canonical_json(manifest) + "\n").encode()).hexdigest()
        if manifest_sha256 != actual_manifest_sha256: raise LiveOrderSafetyError("manifest SHA-256 does not match immutable manifest bytes")

    @staticmethod
    def _fresh_private_checks(client: Any, intent: OrderIntent) -> None:
        info = client.credential_info()
        if info.classification is not CredentialClassification.TRADE_CAPABLE: raise LiveOrderSafetyError("fresh credential classification is not TRADE_CAPABLE")
        account = client.account_info()
        if not all((account.unified_margin_status, account.margin_mode, account.spot_hedging_status, account.updated_time)): raise LiveOrderSafetyError("fresh account context is incomplete")
        balances = {row.coin: row for row in client.wallet_balances()}
        if "BTC" not in balances or "USDT" not in balances: raise LiveOrderSafetyError("fresh BTC/USDT wallet evidence is incomplete")
        if any(row.has_liability for row in balances.values()): raise LiveOrderSafetyError("fresh wallet liability detected")
        available = balances["USDT"].available_for_spot_quote_buy
        if available is None or Decimal(str(available)) < intent.quote_amount_usdt: raise LiveOrderSafetyError("fresh authoritative exact Spot quote-buy availability is unavailable or insufficient")
        rules = client.instrument_rules()
        rules.validate_quote(intent.quote_amount_usdt)
        if client.submission_state(intent.client_order_id) != "conclusively_absent": raise LiveOrderSafetyError("fresh order reconciliation is not conclusively absent")

    def _parse_response(self, intent: OrderIntent, run_id: str, response: HttpResponse, post_ack_reconciler: PostAckReconciler | None) -> tuple[SubmissionOutcome, ReconciliationEvidence | None]:
        if response.status >= 500: return self._outcome(intent, run_id, "ambiguous", msg="server response may have followed submission", category="transport_ambiguous"), None
        if response.status >= 400: return self._outcome(intent, run_id, "rejected_by_exchange", msg="HTTP response rejected submission", category="exchange_rejected"), None
        try: body = json.loads(response.body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError): return self._outcome(intent, run_id, "ambiguous", msg="malformed order response", category="reconciliation_required"), None
        if not isinstance(body, dict): return self._outcome(intent, run_id, "ambiguous", msg="malformed order response", category="reconciliation_required"), None
        code, msg, result = body.get("retCode"), body.get("retMsg"), body.get("result")
        if code != 0:
            state = "rejected_by_exchange" if response.status < 500 else "ambiguous"
            return self._outcome(intent, run_id, state, code=code if isinstance(code, int) else None, msg=str(msg) if msg is not None else None, category="exchange_rejected" if state == "rejected_by_exchange" else "reconciliation_required"), None
        if not isinstance(result, dict) or not isinstance(result.get("orderId"), str) or result.get("orderLinkId") != intent.client_order_id:
            return self._outcome(intent, run_id, "ambiguous", code=code if isinstance(code, int) else None, msg="acknowledgement identity is contradictory", order_id=result.get("orderId") if isinstance(result, dict) else None, returned_link=result.get("orderLinkId") if isinstance(result, dict) else None, category="reconciliation_required"), None
        # Never reuse pre-submit absence after an ACK. The ACK crosses an
        # evidence boundary, so read back through a separate fresh reconciler.
        reconciliation = "ambiguous"
        evidence: ReconciliationEvidence | None = None
        if post_ack_reconciler is not None:
            try:
                fresh_state = post_ack_reconciler.reconcile_after_ack(intent.client_order_id)
                if isinstance(fresh_state, ReconciliationEvidence):
                    evidence = fresh_state
                    reconciliation = fresh_state.state
                else:
                    reconciliation = "confirmed" if fresh_state == "confirmed" else "ambiguous"
            except Exception:
                reconciliation = "ambiguous"
        return self._outcome(intent, run_id, "acknowledged", code=code, msg=str(msg) if msg is not None else None, order_id=result["orderId"], returned_link=result["orderLinkId"], reconciliation=reconciliation, category="confirmed_execution" if evidence and evidence.state == "confirmed" and evidence.fills else "reconciliation_required"), evidence
