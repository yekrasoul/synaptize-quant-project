"""Durable Bybit Spot submission evidence and reconciliation.

This module is deliberately not a ledger writer.  It records immutable
submission evidence before and after the network boundary and turns fresh
Bybit order/fill reads into a fail-closed reconciliation result.  A persisted
attempt always blocks another submission for the same client order identity;
the caller must resolve it through the existing confirmation-only ledger path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from .artifacts import ArtifactStore, ArtifactType, make_run_id
from .bybit_spot_transport import (
    SpotMarketBuyRequest,
    request_fingerprint,
)
from .private_bybit import (
    BybitPrivateReadClient,
    ExecutionFill,
    OrderState,
)


RECONCILIATION_STATES = frozenset({
    "SUBMISSION_UNKNOWN", "ACCEPTED", "NEW", "PARTIALLY_FILLED", "FILLED",
    "CANCELLED", "REJECTED", "UNRESOLVED",
})


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be UTC-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SubmissionContext:
    """Immutable identifiers needed by the existing submission schemas."""

    canary_id: str
    approval_id: str
    manifest_sha256: str
    decision_id: str
    order_intent_id: str
    run_id: str


@dataclass(frozen=True)
class ReconciliationResult:
    state: str
    client_order_id: str
    order_id: str | None
    order_link_id: str
    executed_btc: Decimal
    executed_usdt: Decimal
    fees: tuple[dict[str, str], ...]
    execution_ids: tuple[str, ...]
    final_order_status: str
    reason: str

    @property
    def ledger_confirmation_allowed(self) -> bool:
        return (
            self.state == "FILLED"
            and self.order_id is not None
            and self.executed_btc > 0
            and self.executed_usdt > 0
            and bool(self.execution_ids)
        )


class DurableSubmissionJournal:
    """Append-only persistence adapter over the canonical ``ArtifactStore``."""

    def __init__(self, root: Path | str) -> None:
        self.store = ArtifactStore(Path(root))

    @staticmethod
    def _event_run_id(now: datetime, attempt_id: str, event: str) -> str:
        suffix = sha256(f"{attempt_id}|{event}|{_timestamp(now)}".encode()).hexdigest()[:24]
        return make_run_id(now, f"submit-{event.lower()}-{suffix}")

    def has_prior_attempt(self, client_order_id: str) -> bool:
        """Return true for any verified or corrupt prior attempt evidence."""
        root = self.store.root / "order_submission_attempts"
        if not root.exists():
            return False
        for path in root.glob("*/*/*/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return True
            if isinstance(payload, dict) and payload.get("client_order_id") == client_order_id:
                return True
        return False

    def has_unresolved_attempt(self) -> bool:
        """Return true when any prior attempt lacks terminal reconciliation."""
        attempts = self.store.root / "order_submission_attempts"
        if not attempts.exists():
            return False
        identities: set[str] = set()
        for path in attempts.glob("*/*/*/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return True
            if isinstance(payload, dict) and isinstance(payload.get("client_order_id"), str):
                identities.add(payload["client_order_id"])
        reconciliations = self.store.root / "submission_reconciliations"
        terminal = {"FILLED", "CANCELLED", "REJECTED", "confirmed"}
        for identity in identities:
            states: list[str] = []
            paths = reconciliations.glob("*/*/*/*.json") if reconciliations.exists() else ()
            for path in paths:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    return True
                if isinstance(payload, dict) and payload.get("client_order_id") == identity:
                    states.append(str(payload.get("reconciliation_state")))
            if not states or states[-1] not in terminal:
                return True
        return False

    def persist_before_post(
        self,
        request: SpotMarketBuyRequest,
        context: SubmissionContext,
        *,
        attempt_id: str,
        now: datetime,
    ) -> None:
        if self.has_prior_attempt(request.client_order_id):
            raise ValueError("prior submission attempt exists; reconciliation required")
        self.store.persist(
            ArtifactType.ORDER_SUBMISSION_ATTEMPT,
            {
                "schema_version": "5.4.0",
                "run_id": context.run_id,
                "canary_id": context.canary_id,
                "approval_id": context.approval_id,
                "manifest_sha256": context.manifest_sha256,
                "decision_id": context.decision_id,
                "order_intent_id": context.order_intent_id,
                "client_order_id": request.client_order_id,
                "approved_amount_usdt": str(request.quote_amount_usdt),
                "request_fingerprint": request_fingerprint(request),
                "exchange": "Bybit",
                "market_type": "spot",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "order_type": "Market",
                "created_at_utc": _timestamp(now),
                "state": "prepared",
                "no_order_executed": True,
                "attempt_id": attempt_id,
            },
            run_id=self._event_run_id(now, attempt_id, "PRE")
        )

    def persist_outcome(
        self,
        request: SpotMarketBuyRequest,
        context: SubmissionContext,
        *,
        attempt_id: str,
        now: datetime,
        state: str,
        ret_code: int | None,
        ret_msg: str | None,
        order_id: str | None,
        reconciliation_state: str,
    ) -> None:
        outcome_state = {
            "ACCEPTED": "acknowledged",
            "REJECTED": "rejected_by_exchange",
            "RECONCILIATION_REQUIRED": "ambiguous",
            "BLOCKED": "blocked",
        }.get(state, "ambiguous")
        mapped_reconciliation = {
            "FILLED": "confirmed",
            "PARTIALLY_FILLED": "partial",
            "ACCEPTED": "ACCEPTED",
            "SUBMISSION_UNKNOWN": "ambiguous",
            "UNRESOLVED": "ambiguous",
        }.get(reconciliation_state, "none")
        self.store.persist(
            ArtifactType.ORDER_SUBMISSION_OUTCOME,
            {
                "schema_version": "5.4.0",
                "run_id": context.run_id,
                "decision_id": context.decision_id,
                "order_intent_id": context.order_intent_id,
                "client_order_id": request.client_order_id,
                "state": outcome_state,
                "completed_at_utc": _timestamp(now),
                "ret_code": ret_code,
                "ret_msg": ret_msg,
                "order_id": order_id,
                "returned_order_link_id": request.client_order_id if order_id else None,
                "reconciliation_state": mapped_reconciliation,
                "ledger_not_mutated": True,
                "outcome_category": {
                    "ACCEPTED": "acknowledgement_received",
                    "REJECTED": "exchange_rejected",
                    "RECONCILIATION_REQUIRED": "transport_ambiguous",
                    "BLOCKED": "request_not_sent",
                }.get(state, "reconciliation_required"),
            },
            run_id=self._event_run_id(now, attempt_id, "OUTCOME"),
        )

    def persist_reconciliation(
        self,
        request: SpotMarketBuyRequest,
        context: SubmissionContext,
        *,
        attempt_id: str,
        now: datetime,
        result: ReconciliationResult,
    ) -> None:
        if result.state not in RECONCILIATION_STATES:
            raise ValueError("unknown reconciliation state")
        fills = []
        for fee in result.fees:
            fills.append({
                "exec_id": fee["exec_id"], "order_id": result.order_id or "UNKNOWN",
                "order_link_id": request.client_order_id, "category": "spot", "symbol": "BTCUSDT",
                "quantity_btc": fee["quantity_btc"], "quote_value_usdt": fee["quote_value_usdt"],
                "average_price_usdt": fee["average_price_usdt"], "executed_at_utc": fee["executed_at_utc"],
                "fee": fee["fee"], "fee_asset": fee["fee_asset"],
            })
        self.store.persist(
            ArtifactType.SUBMISSION_RECONCILIATION,
            {
                "schema_version": "5.4.1",
                "run_id": context.run_id,
                "decision_id": context.decision_id,
                "order_intent_id": context.order_intent_id,
                "canary_id": context.canary_id,
                "approval_id": context.approval_id,
                "client_order_id": request.client_order_id,
                "order_id": result.order_id,
                "reconciliation_state": result.state,
                "reconciled_at_utc": _timestamp(now),
                "fills": fills,
                "source": "Bybit private order/fill reconciliation",
                "ledger_mutated": False,
            },
            run_id=self._event_run_id(now, attempt_id, "RECONCILIATION"),
        )


class BybitSubmissionReconciler:
    """Fresh read-only order and execution reconciliation."""

    def __init__(self, read_client: BybitPrivateReadClient) -> None:
        self.read_client = read_client

    @staticmethod
    def _fees(fills: tuple[ExecutionFill, ...]) -> tuple[dict[str, str], ...]:
        return tuple({
            "exec_id": fill.exec_id,
            "quantity_btc": format(fill.exec_qty, "f"),
            "quote_value_usdt": format(fill.exec_value, "f"),
            "average_price_usdt": format(fill.exec_price, "f"),
            "executed_at_utc": fill.exec_time,
            "fee": format(fill.exec_fee, "f"),
            "fee_asset": fill.fee_asset,
        } for fill in fills)

    def reconcile(self, client_order_id: str, *, expected_order_id: str | None = None, expected_quote_amount: Decimal | None = None) -> ReconciliationResult:
        try:
            order = self.read_client.lookup_order(client_order_id)
            fills = self.read_client.executions(client_order_id)
        except Exception:
            return ReconciliationResult("UNRESOLVED", client_order_id, expected_order_id, client_order_id, Decimal("0"), Decimal("0"), (), (), "UNKNOWN", "reconciliation evidence unavailable")
        if order.order_link_id != client_order_id or (expected_order_id and order.order_id != expected_order_id):
            return ReconciliationResult("UNRESOLVED", client_order_id, order.order_id, client_order_id, Decimal("0"), Decimal("0"), (), (), order.raw_status, "order identity conflict")
        if any(fill.order_link_id != client_order_id or fill.order_id != order.order_id or fill.category != "spot" or fill.symbol != "BTCUSDT" for fill in fills):
            return ReconciliationResult("UNRESOLVED", client_order_id, order.order_id, client_order_id, Decimal("0"), Decimal("0"), (), (), order.raw_status, "execution identity conflict")
        executed_btc = sum((fill.exec_qty for fill in fills), Decimal("0"))
        executed_usdt = sum((fill.exec_value for fill in fills), Decimal("0"))
        fee_rows = self._fees(fills)
        ids = tuple(fill.exec_id for fill in fills)
        state = {
            OrderState.NOT_FOUND: "SUBMISSION_UNKNOWN",
            OrderState.ACTIVE: "NEW",
            OrderState.PARTIALLY_FILLED: "PARTIALLY_FILLED",
            OrderState.FILLED: "FILLED",
            OrderState.CANCELLED: "CANCELLED",
            OrderState.REJECTED: "REJECTED",
            OrderState.AMBIGUOUS: "UNRESOLVED",
        }[order.state]
        if state == "FILLED" and not fills:
            state = "UNRESOLVED"
        if state == "PARTIALLY_FILLED" and not fills:
            state = "UNRESOLVED"
        if state == "FILLED" and executed_btc != order.executed_qty:
            state = "UNRESOLVED"
        if state == "FILLED" and executed_usdt != order.executed_value:
            state = "UNRESOLVED"
        if state == "FILLED" and expected_quote_amount is not None and executed_usdt != expected_quote_amount:
            state = "UNRESOLVED"
        return ReconciliationResult(state, client_order_id, order.order_id, client_order_id, executed_btc, executed_usdt, fee_rows, ids, order.raw_status, "fresh Bybit order and execution evidence")


__all__ = ["BybitSubmissionReconciler", "DurableSubmissionJournal", "ReconciliationResult", "SubmissionContext", "RECONCILIATION_STATES"]
