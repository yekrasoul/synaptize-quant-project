"""Fail-closed Phase 5.1 execution foundation.

This module deliberately contains no order-create operation.  It models the
boundary immediately before submission and the read-only reconciliation rules
needed by a future adapter.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Protocol

from .ledger import confirmed_executions, read_executions
from .models import Execution
from .schemas import validate_artifact
from .config import ExecutionConfig, load_execution_config

APPROVED_STRATEGY = "btc_adaptive_dca_v1"
APPROVED_VERSION = "1.0.0"
MIN_REMAINING = Decimal("10")
CLIENT_ID_RE = re.compile(r"^dca-[a-f0-9]{32}$")


class ExecutionSafetyError(ValueError):
    pass


@dataclass(frozen=True)
class OrderIntent:
    schema_version: str
    strategy_id: str
    strategy_version: str
    run_id: str
    decision_id: str
    order_intent_id: str
    created_at_utc: str
    exchange: str
    market_type: str
    symbol: str
    side: str
    quote_amount_usdt: Decimal
    expected_mode: str
    client_order_id: str
    monthly_spent_before_usd: Decimal
    remaining_budget_before_usd: Decimal
    safety_validation_status: str
    live_execution_requested: bool
    live_execution_enabled: bool
    no_order_executed: bool = True

    def to_dict(self) -> dict[str, Any]:
        result = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}
        result["quote_amount_usd"] = result.pop("quote_amount_usdt")
        return result


@dataclass(frozen=True)
class SafetyValidationResult:
    schema_version: str
    run_id: str
    status: str
    reasons: tuple[str, ...] = ()
    checked_at_utc: str = ""
    monthly_spent_recomputed_usd: Decimal = Decimal("0")
    remaining_budget_recomputed_usd: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "run_id": self.run_id, "status": self.status,
                "reasons": list(self.reasons), "checked_at_utc": self.checked_at_utc,
                "monthly_spent_recomputed_usd": str(self.monthly_spent_recomputed_usd),
                "remaining_budget_recomputed_usd": str(self.remaining_budget_recomputed_usd)}


@dataclass(frozen=True)
class OrderSubmission:
    schema_version: str
    client_order_id: str
    state: str
    no_order_executed: bool = True

    def to_dict(self) -> dict[str, Any]: return dict(self.__dict__)


@dataclass(frozen=True)
class Fill:
    fill_id: str
    quantity_btc: Decimal
    quote_value_usdt: Decimal
    confirmed: bool


@dataclass(frozen=True)
class ReconciliationResult:
    schema_version: str
    client_order_id: str
    status: str
    fills: tuple[Fill, ...] = ()
    ambiguous: bool = False

    @property
    def confirmed_quote_value_usdt(self) -> Decimal:
        return sum((f.quote_value_usdt for f in self.fills if f.confirmed), Decimal("0"))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "client_order_id": self.client_order_id,
                "status": self.status, "ambiguous": self.ambiguous,
                "fills": [{"fill_id": f.fill_id, "quantity_btc": str(f.quantity_btc),
                            "quote_value_usdt": str(f.quote_value_usdt), "confirmed": f.confirmed} for f in self.fills]}


def client_order_id(strategy_id: str, decision_id: str, run_id: str) -> str:
    raw = f"{strategy_id}|{decision_id}|{run_id}".encode()
    value = "dca-" + hashlib.sha256(raw).hexdigest()[:32]
    assert CLIENT_ID_RE.fullmatch(value)
    return value


def make_order_intent(decision: Mapping[str, Any], *, run_id: str, created_at_utc: str,
                      live_execution_requested: bool = False) -> OrderIntent:
    amount = Decimal(str(decision["final_purchase_usd"]))
    decision_id = str(decision["decision_id"])
    return OrderIntent("5.1.0", str(decision["strategy_id"]), str(decision["strategy_version"]),
        run_id, decision_id, f"intent_{decision_id}_{run_id}", created_at_utc, "Bybit", "spot", "BTCUSDT", "Buy",
        amount, "recommendation_only", client_order_id(str(decision["strategy_id"]), decision_id, run_id),
        Decimal(str(decision.get("monthly_spent_before_usd", 0))), Decimal(str(decision.get("remaining_budget_before_usd", 0))),
        "pending", live_execution_requested, False, True)


def validate_spot_instrument(exchange: str, market_type: str, symbol: str) -> None:
    if exchange != "Bybit" or market_type.lower() != "spot" or symbol != "BTCUSDT":
        raise ExecutionSafetyError("only Bybit Spot BTCUSDT is approved")
    if any(token in symbol.upper() for token in ("PERP", "FUT", "MARGIN", ".P")):
        raise ExecutionSafetyError("derivatives, margin, and leveraged instruments are prohibited")


@dataclass(frozen=True)
class InstrumentRules:
    quote_minimum: Decimal
    base_quantity_minimum: Decimal
    quantity_step: Decimal
    price_tick_size: Decimal
    market_buy_allowed: bool
    min_notional: Decimal | None = None

    def validate_quote(self, amount: Decimal) -> None:
        for value in (self.quote_minimum, self.base_quantity_minimum, self.quantity_step, self.price_tick_size):
            if not value.is_finite() or value <= 0: raise ExecutionSafetyError("instrument metadata is malformed")
        if not isinstance(self.market_buy_allowed, bool) or not self.market_buy_allowed:
            raise ExecutionSafetyError("market buys are not supported by instrument metadata")
        if amount < self.quote_minimum or (self.min_notional is not None and amount < self.min_notional):
            raise ExecutionSafetyError("order amount violates exchange minimum")


def _metadata_decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExecutionSafetyError(f"{label} is malformed") from exc
    if not parsed.is_finite() or parsed <= 0: raise ExecutionSafetyError(f"{label} is malformed")
    return parsed


def parse_bybit_spot_instrument_info(payload: Mapping[str, Any]) -> InstrumentRules:
    """Parse one authoritative Bybit Spot instrument-info response; never guesses."""
    try:
        result = payload["result"]
        if payload.get("retCode") != 0 or result.get("category") != "spot": raise KeyError
        entries = result["list"]
        if not isinstance(entries, list) or len(entries) != 1: raise KeyError
        row = entries[0]
        if row.get("symbol") != "BTCUSDT" or row.get("baseCoin") != "BTC" or row.get("quoteCoin") != "USDT": raise KeyError
        lot = row["lotSizeFilter"]
        price = row["priceFilter"]
        market_buy = row["marketBuyAllowed"] if "marketBuyAllowed" in row else row["market_buy_allowed"]
        minimum = lot.get("minOrderAmt", lot.get("min_order_amount"))
        if minimum is None: raise KeyError
        if not isinstance(market_buy, bool): raise KeyError
        return InstrumentRules(_metadata_decimal(minimum, "minOrderAmt"),
            _metadata_decimal(lot["minOrderQty"], "minOrderQty"),
            _metadata_decimal(lot.get("qtyStep", lot.get("basePrecision")), "qtyStep"),
            _metadata_decimal(price["tickSize"], "tickSize"), bool(market_buy),
            _metadata_decimal(minimum, "minOrderAmt"))
    except (KeyError, TypeError, AttributeError) as exc:
        raise ExecutionSafetyError("authoritative Bybit Spot instrument metadata is missing or malformed") from exc


class InstrumentMetadataProvider(Protocol):
    def get_rules(self, exchange: str, market_type: str, symbol: str) -> InstrumentRules: ...


class JsonInstrumentMetadataProvider:
    def __init__(self, payload: Mapping[str, Any]): self.payload = payload
    def get_rules(self, exchange: str, market_type: str, symbol: str) -> InstrumentRules:
        if (exchange, market_type, symbol) != ("Bybit", "spot", "BTCUSDT"): raise ExecutionSafetyError("unapproved instrument identity")
        return parse_bybit_spot_instrument_info(self.payload)


VALID_SUBMISSION_STATES = {"none", "conclusively_absent", "ambiguous", "confirmed"}


class SubmissionState(Protocol):
    def decision_state(self, decision_id: str) -> str: ...
    def client_order_state(self, client_order_id: str) -> str: ...


class SubmissionEvidenceStore:
    """Read-only separate evidence store for attempts/reconciliation, not the ledger."""
    def __init__(self, path: Path):
        self.records: list[Mapping[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("state") not in VALID_SUBMISSION_STATES: raise ExecutionSafetyError("invalid submission evidence")
                self.records.append(record)
    def _state(self, key: str, value: str) -> str:
        matches = {record["state"] for record in self.records if record.get(key) == value}
        if not matches: return "none"
        if len(matches) != 1: return "unknown"
        return next(iter(matches))
    def decision_state(self, decision_id: str) -> str: return self._state("decision_id", decision_id)
    def client_order_state(self, client_order_id: str) -> str: return self._state("client_order_id", client_order_id)


class NoSubmissionEvidence:
    def decision_state(self, decision_id: str) -> str: return "none"
    def client_order_state(self, client_order_id: str) -> str: return "none"


def recompute_monthly_spend(ledger_path, calendar_month: str) -> Decimal:
    executions = confirmed_executions(read_executions(ledger_path))
    return sum((e.executed_usd for e in executions if e.executed_at_utc[:7] == calendar_month), Decimal("0"))


def validate_execution_safety(intent: OrderIntent, decision: Mapping[str, Any], *, ledger_path,
                              calendar_month: str, execution_config: ExecutionConfig | None = None,
                              instrument_provider: InstrumentMetadataProvider | None = None,
                              submission_state: SubmissionState | None = None) -> SafetyValidationResult:
    reasons: list[str] = []
    if execution_config is None:
        try: execution_config = load_execution_config()
        except Exception as exc:
            return SafetyValidationResult("5.1.0", intent.run_id, "rejected", (f"execution config unreadable: {exc}",), datetime.now(UTC).isoformat().replace("+00:00", "Z"), Decimal("0"), Decimal("0"))
    if (intent.exchange, intent.market_type, intent.symbol) != (execution_config.exchange, execution_config.market_type, execution_config.symbol): reasons.append("intent market identity does not match execution config")
    if execution_config.kill_switch: reasons.append("kill switch is active")
    if not execution_config.live_execution_enabled: reasons.append("live execution is disabled")
    if execution_config.explicit_live_approval_required: reasons.append("explicit live approval is missing")
    try: validate_spot_instrument(intent.exchange, intent.market_type, intent.symbol)
    except ExecutionSafetyError as exc: reasons.append(str(exc))
    if intent.strategy_id != APPROVED_STRATEGY or intent.strategy_version != APPROVED_VERSION: reasons.append("strategy is not approved V1")
    if intent.side != "Buy": reasons.append("side must be Buy")
    if intent.quote_amount_usdt <= 0: reasons.append("final purchase must be positive")
    if not intent.no_order_executed: reasons.append("intent has an invalid execution state")
    if intent.live_execution_enabled != execution_config.live_execution_enabled: reasons.append("live guard does not match configuration")
    try:
        if Decimal(str(decision["final_purchase_usd"])) != intent.quote_amount_usdt: reasons.append("amount does not match approved Decision")
    except Exception: reasons.append("Decision amount is invalid")
    try:
        timestamp = datetime.fromisoformat(intent.created_at_utc.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0): raise ValueError
        if timestamp.strftime("%Y-%m") != calendar_month: reasons.append("requested calendar month does not match run context")
    except (TypeError, ValueError): reasons.append("run context timestamp is invalid")
    if submission_state is None:
        try: submission_state = LedgerSubmissionState(ledger_path)
        except Exception as exc: reasons.append(f"execution state unreadable: {exc}")
    if submission_state is not None:
        for label, state in (("Decision", submission_state.decision_state(intent.decision_id)), ("client_order_id", submission_state.client_order_state(intent.client_order_id))):
            if state == "confirmed": reasons.append(f"{label} was previously confirmed")
            elif state == "ambiguous": reasons.extend(("REJECT NEW SUBMISSION", "RECONCILIATION REQUIRED"))
            elif state not in VALID_SUBMISSION_STATES: reasons.append("submission evidence state is invalid")
    try:
        spent = recompute_monthly_spend(ledger_path, calendar_month)
    except Exception as exc: reasons.append(f"canonical ledger unreadable: {exc}"); spent = Decimal("0")
    cap = execution_config.monthly_cap_usd if execution_config else Decimal("0")
    remaining = cap - spent
    if spent + intent.quote_amount_usdt > cap: reasons.append("monthly cap would be exceeded")
    if remaining < MIN_REMAINING: reasons.append("remaining budget is below $10")
    if instrument_provider is None: reasons.append("instrument metadata is unavailable")
    else:
        try: instrument_provider.get_rules(intent.exchange, intent.market_type, intent.symbol).validate_quote(intent.quote_amount_usdt)
        except ExecutionSafetyError as exc: reasons.append(str(exc))
    return SafetyValidationResult("5.1.0", intent.run_id, "rejected" if reasons else "approved", tuple(reasons), datetime.now(UTC).isoformat().replace("+00:00", "Z"), spent, remaining)


def reconcile_fills(client_id: str, fills: list[Fill], *, ambiguous: bool = False) -> ReconciliationResult:
    status = "ambiguous" if ambiguous else ("confirmed" if any(f.confirmed for f in fills) else "zero_fill")
    return ReconciliationResult("5.1.0", client_id, status, tuple(fills), ambiguous)


def signing_string(timestamp: str, api_key: str, recv_window: str, body: str) -> str:
    return timestamp + api_key + recv_window + body


def sign_request(timestamp: str, api_key: str, api_secret: str, recv_window: str, body: str) -> str:
    return hmac.new(api_secret.encode(), signing_string(timestamp, api_key, recv_window, body).encode(), hashlib.sha256).hexdigest()


class ReadOnlyBybitClient(Protocol):
    def instrument_info(self, symbol: str) -> Mapping[str, Any]: ...
    def order_status(self, client_order_id: str) -> Mapping[str, Any] | None: ...


def ambiguous_submission_policy() -> str:
    return "mark ambiguous; reconcile by client_order_id; only retry after conclusive absence"
