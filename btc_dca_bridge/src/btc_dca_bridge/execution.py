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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Mapping, Protocol

from .ledger import confirmed_executions, read_executions
from .models import Execution
from .schemas import validate_artifact

APPROVED_STRATEGY = "btc_adaptive_dca_v1"
APPROVED_VERSION = "1.0.0"
MONTHLY_CAP = Decimal("500")
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
    status: str
    reasons: tuple[str, ...] = ()
    checked_at_utc: str = ""
    monthly_spent_recomputed_usd: Decimal = Decimal("0")
    remaining_budget_recomputed_usd: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "status": self.status,
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
                      monthly_spent_usd: Decimal, live_execution_requested: bool = False) -> OrderIntent:
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
        if amount < self.quote_minimum or (self.min_notional is not None and amount < self.min_notional):
            raise ExecutionSafetyError("order amount violates exchange minimum")


def recompute_monthly_spend(ledger_path, calendar_month: str) -> Decimal:
    executions = confirmed_executions(read_executions(ledger_path))
    return sum((e.executed_usd for e in executions if e.executed_at_utc[:7] == calendar_month), Decimal("0"))


def validate_execution_safety(intent: OrderIntent, decision: Mapping[str, Any], *, ledger_path,
                              calendar_month: str, kill_switch: bool = True,
                              live_execution_enabled: bool = False,
                              explicit_live_approval: bool = False,
                              instrument_rules: InstrumentRules | None = None,
                              previously_confirmed_decision_ids: set[str] | None = None) -> SafetyValidationResult:
    reasons: list[str] = []
    try: validate_spot_instrument(intent.exchange, intent.market_type, intent.symbol)
    except ExecutionSafetyError as exc: reasons.append(str(exc))
    if intent.strategy_id != APPROVED_STRATEGY or intent.strategy_version != APPROVED_VERSION: reasons.append("strategy is not approved V1")
    if intent.side != "Buy": reasons.append("side must be Buy")
    if intent.quote_amount_usdt <= 0: reasons.append("final purchase must be positive")
    if kill_switch: reasons.append("kill switch is active")
    if not live_execution_enabled: reasons.append("live execution is disabled")
    if not explicit_live_approval: reasons.append("explicit live approval is missing")
    if not intent.no_order_executed: reasons.append("intent has an invalid execution state")
    if intent.live_execution_enabled != live_execution_enabled: reasons.append("live guard does not match configuration")
    if decision.get("final_purchase_usd") != int(intent.quote_amount_usdt): reasons.append("amount does not match approved Decision")
    if previously_confirmed_decision_ids and intent.decision_id in previously_confirmed_decision_ids: reasons.append("Decision was previously executed")
    try:
        spent = recompute_monthly_spend(ledger_path, calendar_month)
    except Exception as exc: reasons.append(f"canonical ledger unreadable: {exc}"); spent = Decimal("0")
    remaining = MONTHLY_CAP - spent
    if spent + intent.quote_amount_usdt > MONTHLY_CAP: reasons.append("monthly cap would be exceeded")
    if remaining < MIN_REMAINING: reasons.append("remaining budget is below $10")
    if instrument_rules:
        try: instrument_rules.validate_quote(intent.quote_amount_usdt)
        except ExecutionSafetyError as exc: reasons.append(str(exc))
    return SafetyValidationResult("5.1.0", "rejected" if reasons else "approved", tuple(reasons), datetime.now(UTC).isoformat().replace("+00:00", "Z"), spent, remaining)


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
