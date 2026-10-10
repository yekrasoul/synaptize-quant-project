"""Phase 5.4C explicit, one-shot human authorization ceremony.

This module only persists authorization evidence.  It does not enable the
execution configuration, construct a transport, submit an order, or mutate
the ledger.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .artifacts import ArtifactStore, ArtifactType
from .config import load_execution_config
from .errors import ArtifactNotFoundError
from .live_readiness import LiveExecutionApproval, LiveReadinessError, canonical_fingerprint

AUTHORIZATION_TTL = timedelta(minutes=5)
AUTHORIZATION_SCHEMA_VERSION = "5.4.0C"
_CONFIRMATION = re.compile(r"^AUTHORIZE BYBIT SPOT BTCUSDT BUY \$([0-9]+(?:\.[0-9]+)?) (dca-[a-f0-9]{32})$")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise LiveReadinessError("authorization time must be UTC-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LiveReadinessError(f"{label} is invalid") from exc
    if not result.is_finite() or result <= 0:
        raise LiveReadinessError(f"{label} must be positive and finite")
    return result


def artifact_sha256(payload: Mapping[str, Any]) -> str:
    """Match ArtifactStore's canonical JSON-plus-newline digest."""
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def operator_confirmation_digest(confirmation: str) -> str:
    if not isinstance(confirmation, str) or not _CONFIRMATION.fullmatch(confirmation):
        raise LiveReadinessError("confirmation must exactly authorize the Bybit Spot BTCUSDT amount and client order ID")
    return hashlib.sha256(confirmation.encode("utf-8")).hexdigest()


def validate_confirmation(confirmation: str, *, amount: Any, client_order_id: str) -> str:
    digest = operator_confirmation_digest(confirmation)
    match = _CONFIRMATION.fullmatch(confirmation)
    assert match is not None
    if _decimal(match.group(1), "confirmation amount") != _decimal(amount, "approved amount"):
        raise LiveReadinessError("confirmation amount does not match the immutable approval")
    if match.group(2) != client_order_id:
        raise LiveReadinessError("confirmation client order ID does not match the immutable approval")
    return digest


@dataclass(frozen=True)
class RealMoneyAuthorization:
    payload: Mapping[str, Any]

    @property
    def authorization_id(self) -> str:
        return str(self.payload["authorization_id"])

    @property
    def one_shot_identity(self) -> str:
        return str(self.payload["one_shot_identity"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)

    def persist(self, store: ArtifactStore, *, run_id: str):
        return store.persist(ArtifactType.REAL_MONEY_AUTHORIZATION, self.payload, run_id=run_id)

    @classmethod
    def from_approval(
        cls, approval: LiveExecutionApproval, *, approval_sha256: str,
        canary_manifest_id: str, canary_manifest_sha256: str,
        amount: Any, client_order_id: str, calendar_month: str,
        confirmation: str, now_utc: datetime,
    ) -> "RealMoneyAuthorization":
        p = approval.to_dict()
        if p.get("state") != "PREPARED":
            raise LiveReadinessError("LiveExecutionApproval must be PREPARED before authorization")
        if p.get("explicit_authorization_status") != "NOT_AUTHORIZED":
            raise LiveReadinessError("approval preparation status is invalid")
        if p.get("canary_manifest_id") != canary_manifest_id or p.get("canary_manifest_sha256") != canary_manifest_sha256:
            raise LiveReadinessError("CanaryManifest identity does not match the persisted LiveExecutionApproval")
        if p.get("client_order_id") != client_order_id:
            raise LiveReadinessError("client order ID does not match the persisted LiveExecutionApproval")
        if _decimal(p.get("quote_amount_usdt"), "approval amount") != _decimal(amount, "requested amount"):
            raise LiveReadinessError("quote amount does not match the persisted LiveExecutionApproval")
        if p.get("calendar_month") != calendar_month:
            raise LiveReadinessError("UTC calendar month does not match the persisted LiveExecutionApproval")
        confirmation_digest = validate_confirmation(confirmation, amount=amount, client_order_id=client_order_id)
        now = now_utc.astimezone(UTC)
        try:
            approval_expires = datetime.fromisoformat(str(p["expires_at_utc"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as exc:
            raise LiveReadinessError("approval expiry is malformed") from exc
        expires = min(now + AUTHORIZATION_TTL, approval_expires)
        if expires <= now:
            raise LiveReadinessError("LiveExecutionApproval is expired")
        body: dict[str, Any] = {
            "schema_version": AUTHORIZATION_SCHEMA_VERSION,
            "created_at_utc": _timestamp(now), "expires_at_utc": _timestamp(expires),
            "status": "AUTHORIZED", "approval_id": approval.approval_id,
            "approval_sha256": approval_sha256,
            "canary_manifest_id": p["canary_manifest_id"],
            "canary_manifest_sha256": p["canary_manifest_sha256"],
            "decision_id": p["decision_id"], "decision_sha256": p["decision_sha256"],
            "order_intent_id": p["order_intent_id"], "order_intent_sha256": p["order_intent_sha256"],
            "client_order_id": p["client_order_id"], "exact_quote_amount_usdt": p["quote_amount_usdt"],
            "exchange": "Bybit", "market_type": "spot", "symbol": "BTCUSDT",
            "side": "Buy", "order_type": "Market", "market_unit": "quoteCoin",
            "is_leverage": 0, "order_filter": "Order", "calendar_month": p["calendar_month"],
            "repository_commit_sha": p["repository_commit_sha"],
            "execution_safety_config_fingerprint": p["execution_safety_config_fingerprint"],
            "production_contract_fingerprint": p["production_contract_fingerprint"],
            "account_identity_fingerprint": p["account_identity_fingerprint"],
            "one_shot_identity": p["one_shot_identity"],
            "operator_confirmation_digest": confirmation_digest,
            "standing_authorization": False, "consumed": False, "no_order_executed": True,
        }
        body["authorization_id"] = "rmauth-" + canonical_fingerprint({key: value for key, value in body.items() if key != "authorization_id"})[:32]
        return cls(body)


def authorize_persisted_approval(
    *, store: ArtifactStore, approval_id: str, approval_sha256: str,
    canary_manifest_id: str, canary_manifest_sha256: str, amount: Any,
    client_order_id: str, calendar_month: str, confirmation: str,
    run_id: str, now_utc: datetime,
) -> tuple[RealMoneyAuthorization, Any]:
    approval_payload, persisted_sha = store.find_artifact(ArtifactType.LIVE_EXECUTION_APPROVAL, identity_field="approval_id", identity_value=approval_id)
    if persisted_sha != approval_sha256:
        raise LiveReadinessError("approval SHA-256 sidecar does not match the persisted artifact")
    if artifact_sha256(approval_payload) != approval_sha256:
        raise LiveReadinessError("approval bytes are not consistent with the supplied SHA-256")
    approval = LiveExecutionApproval(approval_payload)
    if store.has_submission_artifact(str(approval_payload["decision_id"]), client_order_id, kinds=(ArtifactType.LIVE_EXECUTION_ATTEMPT, ArtifactType.ORDER_SUBMISSION_ATTEMPT, ArtifactType.ORDER_SUBMISSION_OUTCOME)):
        raise LiveReadinessError("submission evidence already exists for this one-shot identity")
    try:
        store.find_artifact(ArtifactType.REAL_MONEY_AUTHORIZATION, identity_field="one_shot_identity", identity_value=str(approval_payload["one_shot_identity"]))
    except ArtifactNotFoundError:
        pass
    else:
        raise LiveReadinessError("a RealMoneyAuthorization already exists for this one-shot identity")
    authorization = RealMoneyAuthorization.from_approval(approval, approval_sha256=approval_sha256, canary_manifest_id=canary_manifest_id, canary_manifest_sha256=canary_manifest_sha256, amount=amount, client_order_id=client_order_id, calendar_month=calendar_month, confirmation=confirmation, now_utc=now_utc)
    return authorization, authorization.persist(store, run_id=run_id)


def verify_authorization_binding(
    approval: LiveExecutionApproval, authorization: Mapping[str, Any] | None,
    *, approval_sha256: str | None, authorization_sha256: str | None,
    now_utc: datetime,
) -> tuple[bool, tuple[str, ...]]:
    if authorization is None:
        return False, ("verified RealMoneyAuthorization is absent",)
    reasons: list[str] = []
    if authorization.get("status") != "AUTHORIZED": reasons.append("authorization is not AUTHORIZED")
    if authorization.get("consumed") is not False: reasons.append("authorization is consumed")
    if authorization.get("standing_authorization") is not False: reasons.append("standing authorization is prohibited")
    if authorization.get("no_order_executed") is not True: reasons.append("authorization safety marker is invalid")
    if approval_sha256 is None or authorization.get("approval_sha256") != approval_sha256: reasons.append("authorization approval SHA does not match")
    if approval_sha256 is not None and artifact_sha256(approval.to_dict()) != approval_sha256: reasons.append("approval SHA does not match canonical approval bytes")
    if authorization_sha256 is None or artifact_sha256(authorization) != authorization_sha256: reasons.append("authorization SHA does not match canonical bytes")
    for key in ("approval_id", "canary_manifest_id", "canary_manifest_sha256", "decision_id", "decision_sha256", "order_intent_id", "order_intent_sha256", "client_order_id", "calendar_month", "repository_commit_sha", "execution_safety_config_fingerprint", "production_contract_fingerprint", "account_identity_fingerprint", "one_shot_identity"):
        if authorization.get(key) != approval.payload.get(key): reasons.append(f"authorization binding mismatch: {key}")
    if authorization.get("exact_quote_amount_usdt") != approval.payload.get("quote_amount_usdt"): reasons.append("authorization amount mismatch")
    if (authorization.get("exchange"), authorization.get("market_type"), authorization.get("symbol"), authorization.get("side"), authorization.get("order_type"), authorization.get("market_unit"), authorization.get("is_leverage"), authorization.get("order_filter")) != ("Bybit", "spot", "BTCUSDT", "Buy", "Market", "quoteCoin", 0, "Order"):
        reasons.append("authorization order contract mismatch")
    try:
        expires = datetime.fromisoformat(str(authorization["expires_at_utc"]).replace("Z", "+00:00"))
        if now_utc.astimezone(UTC) >= expires: reasons.append("authorization is expired")
    except (KeyError, ValueError): reasons.append("authorization expiry is malformed")
    return not reasons, tuple(dict.fromkeys(reasons))


def _not_authorized_status() -> dict[str, Any]:
    """Return the safe status for an installation with no authorization."""
    return {
        "authorization": "NOT_AUTHORIZED",
        "execution": "BLOCKED",
        "live_execution_enabled": False,
        "kill_switch": True,
        "order_submission": "not_implemented",
        "message": "NO ORDER EXECUTED",
    }


def _authorization_candidates(store: ArtifactStore) -> list[tuple[dict[str, Any], str]]:
    """Read every persisted authorization, preserving append-only semantics."""
    root = store.root / "real_money_authorizations"
    candidates: list[tuple[datetime, dict[str, Any], str]] = []
    for path in sorted(root.glob("*/*/*/*.json")):
        parts = path.parts
        try:
            artifact_date = datetime(int(parts[-4]), int(parts[-3]), int(parts[-2]), tzinfo=UTC)
        except (TypeError, ValueError, IndexError) as exc:
            from .errors import ArtifactCorruptError
            raise ArtifactCorruptError("authorization artifact directory date is invalid") from exc
        payload = store.read(ArtifactType.REAL_MONEY_AUTHORIZATION, run_id=path.stem, artifact_date_utc=artifact_date)
        try:
            created = datetime.fromisoformat(str(payload["created_at_utc"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as exc:
            from .errors import ArtifactCorruptError
            raise ArtifactCorruptError("authorization creation timestamp is malformed") from exc
        candidates.append((created, payload, artifact_sha256(payload)))
    candidates.sort(key=lambda item: item[0])
    return [(payload, digest) for _, payload, digest in candidates]


def read_authorization_status(
    *, store: ArtifactStore, authorization_id: str | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Derive safe authorization status without writing artifacts or external state."""
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise LiveReadinessError("status observation time must be UTC-aware")
    if authorization_id is None:
        candidates = _authorization_candidates(store)
        if not candidates:
            return _not_authorized_status()
        payload, authorization_sha256 = candidates[-1]
    else:
        payload, authorization_sha256 = store.find_artifact(
            ArtifactType.REAL_MONEY_AUTHORIZATION,
            identity_field="authorization_id",
            identity_value=authorization_id,
        )

    binding_valid = False
    try:
        approval_payload, approval_sha256 = store.find_artifact(
            ArtifactType.LIVE_EXECUTION_APPROVAL,
            identity_field="approval_id",
            identity_value=str(payload.get("approval_id")),
        )
        binding_valid, _ = verify_authorization_binding(
            LiveExecutionApproval(approval_payload), payload,
            approval_sha256=approval_sha256,
            authorization_sha256=authorization_sha256,
            now_utc=now,
        )
    except ArtifactNotFoundError:
        binding_valid = False

    derived_status = str(payload.get("status", "INVALID"))
    if str(payload.get("status")) == "REVOKED":
        derived_status = "REVOKED"
    elif store.has_submission_artifact(
        str(payload.get("decision_id")), str(payload.get("client_order_id")),
        kinds=(ArtifactType.LIVE_EXECUTION_ATTEMPT, ArtifactType.ORDER_SUBMISSION_ATTEMPT, ArtifactType.ORDER_SUBMISSION_OUTCOME),
    ) or payload.get("consumed") is True:
        derived_status = "CONSUMED"
    else:
        try:
            expires = datetime.fromisoformat(str(payload["expires_at_utc"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            derived_status = "INVALID"
        else:
            if now >= expires:
                derived_status = "EXPIRED"
            elif derived_status not in {"AUTHORIZED", "EXPIRED", "CONSUMED", "REVOKED"}:
                derived_status = "INVALID"

    config = load_execution_config()
    execution_ready = (
        binding_valid and derived_status == "AUTHORIZED"
        and config.live_execution_enabled and not config.kill_switch
        and config.order_submission != "not_implemented"
    )
    result: dict[str, Any] = {
        "authorization": "AUTHORIZED" if binding_valid and derived_status == "AUTHORIZED" else "NOT_AUTHORIZED",
        "status": derived_status,
        "authorization_id": payload.get("authorization_id"),
        "expires_at_utc": payload.get("expires_at_utc"),
        "one_shot_identity": payload.get("one_shot_identity"),
        "exact_quote_amount_usdt": payload.get("exact_quote_amount_usdt"),
        "client_order_id": payload.get("client_order_id"),
        "calendar_month": payload.get("calendar_month"),
        "approval_binding": "VALID" if binding_valid else "INVALID",
        "consumed": derived_status == "CONSUMED",
        "revocation_state": "REVOKED" if derived_status == "REVOKED" else "NOT_REVOKED",
        "execution": "READY" if execution_ready else "BLOCKED",
        "live_execution_enabled": config.live_execution_enabled,
        "kill_switch": config.kill_switch,
        "order_submission": config.order_submission,
        "message": "NO ORDER EXECUTED",
    }
    return result
