"""Durable V2 execution records, independent of legacy ceremony artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

from ..core.models import Asset, Market, OrderIntent, OrderType, Side
from ..core.state import ExecutionState, Order
from ..execution_v2.oms import SubmissionAlreadyClaimed, UNRESOLVED_STATES


class JsonExecutionRepository:
    """One atomically persisted record per client identity.

    The durable ``SUBMISSION_STARTED`` write is the claim immediately before
    any venue network operation. A restart therefore cannot create another
    execution with the same identity.
    """
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, identity: str) -> Path:
        return self.root / f"{hashlib.sha256(identity.encode()).hexdigest()}.json"

    def _claim_path(self, identity: str) -> Path:
        return self.root / f"{hashlib.sha256(identity.encode()).hexdigest()}.submission-claim"

    def _fsync_directory(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _payload(order: Order) -> dict[str, object]:
        i = order.intent
        return {"schema_version": "2.0.0", "state": order.state.value, "venue_order_id": order.venue_order_id, "reason": order.reason,
                "intent": {"intent_id": i.intent_id, "strategy_id": i.strategy_id, "decision_id": i.decision_id, "base": i.market.base.code, "quote": i.market.quote.code, "instrument_type": i.market.instrument_type, "side": i.side.value, "order_type": i.order_type.value, "quote_quantity": str(i.quote_quantity), "preferred_venue": i.preferred_venue, "client_order_id": i.client_order_id, "created_at_utc": i.created_at_utc}}

    @staticmethod
    def _order(payload: object) -> Order:
        if not isinstance(payload, dict) or payload.get("schema_version") != "2.0.0" or not isinstance(payload.get("intent"), dict):
            raise ValueError("execution repository record is corrupt")
        v = payload["intent"]
        try:
            intent = OrderIntent(str(v["intent_id"]), str(v["strategy_id"]), str(v["decision_id"]), Market(Asset(str(v["base"])), Asset(str(v["quote"])), str(v["instrument_type"])), Side(str(v["side"])), OrderType(str(v["order_type"])), Decimal(str(v["quote_quantity"])), str(v["preferred_venue"]), str(v["client_order_id"]), str(v["created_at_utc"]))
            return Order(intent, ExecutionState(str(payload["state"])), payload.get("venue_order_id") if isinstance(payload.get("venue_order_id"), str) else None, payload.get("reason") if isinstance(payload.get("reason"), str) else None)
        except (KeyError, ValueError) as exc:
            raise ValueError("execution repository record is corrupt") from exc

    def _write(self, path: Path, order: Order, exclusive: bool = False) -> None:
        data = json.dumps(self._payload(order), sort_keys=True, separators=(",", ":"))
        if exclusive:
            try: fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc: raise ValueError("duplicate canonical execution identity") from exc
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            self._fsync_directory()
            return
        fd, name = tempfile.mkstemp(prefix=".execution-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, order: Order) -> Order:
        self._write(self._path(order.intent.client_order_id), order, True)
        return order

    def save(self, order: Order) -> Order:
        path = self._path(order.intent.client_order_id)
        if not path.exists(): raise ValueError("execution identity was not durably claimed")
        self._write(path, order)
        return order

    def get(self, client_order_id: str) -> Order | None:
        path = self._path(client_order_id)
        if not path.exists(): return None
        order = self._order(json.loads(path.read_text(encoding="utf-8")))
        # A crash may occur after the exclusive claim file is durable but before
        # the record replace. Treat that identity as submitted-started forever.
        if self._claim_path(client_order_id).exists() and order.state is ExecutionState.RISK_APPROVED:
            return order.transition(ExecutionState.SUBMISSION_STARTED, reason="durable submission claim present")
        return order

    def unresolved(self) -> tuple[Order, ...]:
        return tuple(order for path in self.root.glob("*.json") for order in (self.get(self._order(json.loads(path.read_text(encoding="utf-8"))).intent.client_order_id),) if order is not None and order.state in UNRESOLVED_STATES)

    def claim_submission(self, order: Order) -> Order:
        current = self.get(order.intent.client_order_id)
        if current is None or current.state is not ExecutionState.RISK_APPROVED:
            raise SubmissionAlreadyClaimed("submission already claimed or execution is not risk approved")
        claim = self._claim_path(order.intent.client_order_id)
        claim_payload = json.dumps({"client_order_id": order.intent.client_order_id, "state": ExecutionState.SUBMISSION_STARTED.value}, sort_keys=True, separators=(",", ":"))
        try:
            descriptor = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise SubmissionAlreadyClaimed("submission already claimed; reconciliation required") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(claim_payload); handle.flush(); os.fsync(handle.fileno())
        self._fsync_directory()
        return self.save(current.transition(ExecutionState.SUBMISSION_STARTED))
