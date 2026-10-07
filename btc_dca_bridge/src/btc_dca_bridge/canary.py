"""Read-only Phase 5.4A controlled-canary preparation.

This module prepares an immutable, short-lived package for human review.  It
does not import or instantiate the Phase 5.3 submission transport and has no
HTTP mutating path.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol

from .artifacts import ArtifactStore, ArtifactType
from .config import ExecutionConfig, load_execution_config
from .errors import ArtifactAlreadyExistsError
from .execution import make_order_intent
from .ledger import confirmed_executions, read_executions, validate_calendar_month
from .live_order import SpotMarketBuyRequest
from .private_bybit import AccountInfo, ApiCredentialInfo, CredentialClassification, WalletBalance

CANARY_SCHEMA_VERSION = "5.4.0"
CANARY_EXPIRY = timedelta(minutes=15)
APPROVED_ACTIONS = {"SpotTrade", "OrderEntry", "SpotOrder"}


class CanaryPreparationError(ValueError):
    pass


class ReadOnlyVerificationClient(Protocol):
    def credential_info(self) -> ApiCredentialInfo: ...
    def account_info(self) -> AccountInfo: ...
    def wallet_balances(self) -> tuple[WalletBalance, ...]: ...
    def instrument_rules(self): ...
    def submission_state(self, client_order_id: str) -> str: ...


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CanaryPreparationError("canary clock must be UTC-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CanaryPreparationError(f"{label} is invalid") from exc
    if not parsed.is_finite():
        raise CanaryPreparationError(f"{label} is invalid")
    return parsed


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CanaryManifest:
    schema_version: str
    canary_id: str
    run_id: str
    strategy_id: str
    strategy_version: str
    decision_id: str
    order_intent_id: str
    client_order_id: str
    exchange: str
    market_type: str
    symbol: str
    side: str
    order_type: str
    approved_amount_usdt: Decimal
    calendar_month: str
    monthly_spent_usd: Decimal | None
    remaining_budget_usd: Decimal | None
    credential_classification: str | None
    account_mode: str | None
    account_unified_margin_status: str | None
    account_spot_hedging_status: str | None
    account_updated_time: str | None
    wallet_usdt: Decimal | None
    wallet_btc: Decimal | None
    liability_detected: bool | None
    instrument_min_order_amt: Decimal | None
    instrument_qty_step: Decimal | None
    instrument_tick_size: Decimal | None
    pre_submission_state: str
    order_payload_fingerprint: str
    prepared_at_utc: str
    expires_at_utc: str
    live_execution_enabled: bool
    kill_switch: bool
    canary_status: str
    reasons: tuple[str, ...] = ()

    def is_expired(self, *, now_utc: datetime) -> bool:
        current = _timestamp(now_utc)
        return current >= self.expires_at_utc

    def validate_for_execution(self, *, now_utc: datetime) -> None:
        if self.is_expired(now_utc=now_utc):
            raise CanaryPreparationError("CANARY MANIFEST EXPIRED; prepare a new manifest")
        raise CanaryPreparationError("CANARY EXECUTION NOT ENABLED")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"schema_version": self.schema_version, "canary_id": self.canary_id,
            "run_id": self.run_id, "strategy_id": self.strategy_id, "strategy_version": self.strategy_version,
            "decision_id": self.decision_id, "order_intent_id": self.order_intent_id,
            "client_order_id": self.client_order_id, "exchange": self.exchange, "market_type": self.market_type,
            "symbol": self.symbol, "side": self.side, "order_type": self.order_type,
            "approved_amount_usdt": str(self.approved_amount_usdt), "calendar_month": self.calendar_month,
            "monthly_spent_usd": None if self.monthly_spent_usd is None else str(self.monthly_spent_usd),
            "remaining_budget_usd": None if self.remaining_budget_usd is None else str(self.remaining_budget_usd),
            "credential_classification": self.credential_classification, "account_mode": self.account_mode,
            "account_unified_margin_status": self.account_unified_margin_status,
            "account_spot_hedging_status": self.account_spot_hedging_status,
            "account_updated_time": self.account_updated_time,
            "wallet_usdt": None if self.wallet_usdt is None else str(self.wallet_usdt),
            "wallet_btc": None if self.wallet_btc is None else str(self.wallet_btc),
            "liability_detected": self.liability_detected,
            "instrument_min_order_amt": None if self.instrument_min_order_amt is None else str(self.instrument_min_order_amt),
            "instrument_qty_step": None if self.instrument_qty_step is None else str(self.instrument_qty_step),
            "instrument_tick_size": None if self.instrument_tick_size is None else str(self.instrument_tick_size),
            "pre_submission_state": self.pre_submission_state,
            "order_payload_fingerprint": self.order_payload_fingerprint, "prepared_at_utc": self.prepared_at_utc,
            "expires_at_utc": self.expires_at_utc, "live_execution_enabled": self.live_execution_enabled,
            "kill_switch": self.kill_switch, "canary_status": self.canary_status, "reasons": list(self.reasons)}
        return result


@dataclass(frozen=True)
class CanaryPreparationResult:
    manifest: CanaryManifest
    artifact_receipt: Any
    order_payload: Mapping[str, Any]


class CanaryPreparer:
    def __init__(self, *, artifact_store: ArtifactStore, client: ReadOnlyVerificationClient,
                 execution_config: ExecutionConfig | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self.artifact_store = artifact_store
        self.client = client
        self.execution_config = execution_config or load_execution_config()
        self._now = now or (lambda: datetime.now(UTC))

    def prepare(self, decision: Mapping[str, Any], *, run_id: str, calendar_month: str, ledger_path) -> CanaryPreparationResult:
        now = self._now().astimezone(UTC)
        prepared_at = _timestamp(now)
        expires_at = _timestamp(now + CANARY_EXPIRY)
        validate_calendar_month(calendar_month)
        intent = make_order_intent(decision, run_id=run_id, created_at_utc=str(decision["created_at_utc"]))
        amount = _decimal(decision["final_purchase_usd"], "Decision.final_purchase_usd")
        request = SpotMarketBuyRequest(amount, intent.client_order_id)
        payload = request.to_payload()
        payload_fingerprint = _fingerprint(payload)
        canary_id = "canary-" + _fingerprint({"decision_id": intent.decision_id, "order_intent_id": intent.order_intent_id,
                                                "client_order_id": intent.client_order_id,
                                                "order_payload_fingerprint": payload_fingerprint,
                                                "prepared_at_utc": prepared_at})[:32]
        reasons: list[str] = []
        monthly_spent: Decimal | None = None
        remaining: Decimal | None = None
        credential: ApiCredentialInfo | None = None
        account: AccountInfo | None = None
        balances: tuple[WalletBalance, ...] = ()
        rules = None
        pre_state = "unavailable"

        config = self.execution_config
        if config.live_execution_enabled is not False: reasons.append("production live execution must remain disabled")
        if config.kill_switch is not True: reasons.append("production kill switch must remain active")
        if config.order_submission != "not_implemented": reasons.append("production order submission mode is not_implemented invariant")
        if now.strftime("%Y-%m") != calendar_month: reasons.append("requested month is not the current UTC calendar month")
        if intent.created_at_utc[:7] != calendar_month: reasons.append("Decision/run month does not match requested calendar month")
        if intent.strategy_id != "btc_adaptive_dca_v1" or intent.strategy_version != "1.0.0": reasons.append("only V1 Decision is eligible")
        if amount != intent.quote_amount_usdt: reasons.append("Decision amount does not exactly match OrderIntent")
        if amount != amount.to_integral_value() or amount <= 0: reasons.append("V1 amount is not a positive whole-dollar amount")
        if intent.exchange != "Bybit" or intent.market_type != "spot" or intent.symbol != "BTCUSDT" or intent.side != "Buy": reasons.append("order identity is not approved")

        try:
            executions = confirmed_executions(read_executions(ledger_path))
            monthly_spent = sum((execution.executed_usd for execution in executions if execution.executed_at_utc[:7] == calendar_month), Decimal("0"))
            remaining = config.monthly_cap_usd - monthly_spent
            if amount > remaining: reasons.append("monthly cap would be exceeded")
            for execution in executions:
                if execution.payload.get("decision_id") == intent.decision_id or execution.payload.get("client_order_id") == intent.client_order_id:
                    reasons.append("prior confirmed execution requires reconciliation")
        except Exception:
            reasons.append("canonical ledger reread failed")

        try:
            credential = self.client.credential_info()
            if credential.classification is not CredentialClassification.TRADE_CAPABLE:
                reasons.append("credential must be TRADE_CAPABLE")
            if not any(action in APPROVED_ACTIONS for action in credential.permissions.get("Spot", ())):
                reasons.append("credential lacks approved Spot trading scope")
        except Exception:
            reasons.append("credential verification unavailable")
        try:
            account = self.client.account_info()
            if not all((account.unified_margin_status, account.margin_mode, account.spot_hedging_status, account.updated_time)):
                reasons.append("account verification is incomplete")
        except Exception:
            reasons.append("account verification unavailable")
        try:
            balances = self.client.wallet_balances()
            by_coin = {balance.coin: balance for balance in balances}
            usdt, btc = by_coin.get("USDT"), by_coin.get("BTC")
            if usdt is None or btc is None: reasons.append("BTC and USDT wallet balances are required")
            else:
                if usdt.has_liability or btc.has_liability: reasons.append("wallet liability detected")
                available_usdt = usdt.wallet_balance - usdt.locked
                if available_usdt < amount: reasons.append("available USDT is insufficient for exact V1 amount")
        except Exception:
            reasons.append("wallet verification unavailable")
        try:
            rules = self.client.instrument_rules()
            rules.validate_quote(amount)
        except Exception:
            reasons.append("authoritative Spot instrument verification failed")
        try:
            pre_state = self.client.submission_state(intent.client_order_id)
            if pre_state != "conclusively_absent": reasons.append("pre-submission reconciliation is not conclusively absent")
        except Exception:
            pre_state = "ambiguous"
            reasons.append("pre-submission reconciliation unavailable")
        try:
            if self.artifact_store.has_submission_artifact(intent.decision_id, intent.client_order_id):
                reasons.extend(("prior submission artifact exists", "RECONCILIATION REQUIRED"))
        except Exception:
            reasons.append("submission artifact state is unreadable")

        manifest = CanaryManifest(CANARY_SCHEMA_VERSION, canary_id, run_id, intent.strategy_id, intent.strategy_version,
            intent.decision_id, intent.order_intent_id, intent.client_order_id, intent.exchange, intent.market_type,
            intent.symbol, intent.side, "Market", amount, calendar_month, monthly_spent, remaining,
            credential.classification.value if credential else None,
            str(account.margin_mode) if account else None,
            str(account.unified_margin_status) if account else None,
            str(account.spot_hedging_status) if account else None,
            str(account.updated_time) if account else None,
            self._wallet_value(balances, "USDT"), self._wallet_value(balances, "BTC"),
            any(balance.has_liability for balance in balances) if balances else None,
            rules.quote_minimum if rules else None, rules.quantity_step if rules else None,
            rules.price_tick_size if rules else None, pre_state, payload_fingerprint,
            prepared_at, expires_at, config.live_execution_enabled, config.kill_switch,
            "READY_FOR_MANUAL_APPROVAL" if not reasons else "BLOCKED", tuple(dict.fromkeys(reasons)))
        try:
            receipt = self.artifact_store.persist(ArtifactType.CANARY_MANIFEST, manifest, run_id=run_id)
        except ArtifactAlreadyExistsError as exc:
            raise CanaryPreparationError("canary manifest identity already exists; create a new preparation run") from exc
        return CanaryPreparationResult(manifest, receipt, payload)

    @staticmethod
    def _wallet_value(balances: tuple[WalletBalance, ...], coin: str) -> Decimal | None:
        for balance in balances:
            if balance.coin == coin:
                return balance.wallet_balance - balance.locked if coin == "USDT" else balance.wallet_balance
        return None


class CanaryExecutor:
    """Phase 5.4B placeholder; deliberately unreachable in Phase 5.4A."""
    def execute(self, *args: Any, **kwargs: Any) -> None:
        raise CanaryPreparationError("CANARY EXECUTION NOT ENABLED")
