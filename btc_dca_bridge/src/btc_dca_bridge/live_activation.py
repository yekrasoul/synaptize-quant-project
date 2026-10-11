"""Phase 5.4D controlled, one-shot execution activation.

Activation is a short-lived capability artifact, not authorization and not a
transport.  This module contains no Bybit client and no ledger writer.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .artifacts import ArtifactStore, ArtifactType
from .config import ExecutionConfig, load_execution_config
from .errors import ArtifactNotFoundError
from .live_readiness import GateResult, GateStatus, LiveExecutionApproval, LiveReadinessError, canonical_fingerprint
from .real_money_authorization import artifact_sha256, verify_authorization_binding
from .ttl_policy import EXECUTION_ACTIVATION_TTL

ACTIVATION_TTL = EXECUTION_ACTIVATION_TTL
ACTIVATION_SCHEMA_VERSION = "5.4.0D"
_PHRASE = re.compile(r"^ACTIVATE ONE SHOT BYBIT SPOT BTCUSDT BUY \$([0-9]+(?:\.[0-9]+)?) (dca-[a-f0-9]{32})$")


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise LiveReadinessError("activation time must be UTC-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _amount(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LiveReadinessError("activation amount is invalid") from exc
    if not amount.is_finite() or amount <= 0:
        raise LiveReadinessError("activation amount must be positive and finite")
    return amount


def activation_phrase_digest(phrase: str, *, amount: Any, client_order_id: str) -> str:
    if not isinstance(phrase, str) or not _PHRASE.fullmatch(phrase):
        raise LiveReadinessError("activation phrase must exactly authorize the Bybit Spot BTCUSDT amount and client order ID")
    match = _PHRASE.fullmatch(phrase)
    assert match is not None
    if _amount(match.group(1)) != _amount(amount):
        raise LiveReadinessError("activation phrase amount does not match the immutable identity")
    if match.group(2) != client_order_id:
        raise LiveReadinessError("activation phrase client order ID does not match the immutable identity")
    return hashlib.sha256(phrase.encode("utf-8")).hexdigest()


class ExecutionActivation:
    """Immutable persisted capability request for exactly one identity."""

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)

    @property
    def activation_id(self) -> str:
        return str(self.payload["activation_id"])

    @property
    def one_shot_identity(self) -> str:
        return str(self.payload["one_shot_identity"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)

    def persist(self, store: ArtifactStore, *, run_id: str):
        return store.persist(ArtifactType.EXECUTION_ACTIVATION, self.payload, run_id=run_id)

    @classmethod
    def from_authorization(
        cls, authorization: Mapping[str, Any], *, authorization_sha256: str,
        approval: LiveExecutionApproval, approval_sha256: str,
        phrase: str, now_utc: datetime,
    ) -> "ExecutionActivation":
        if authorization.get("status") != "AUTHORIZED" or authorization.get("consumed") is not False:
            raise LiveReadinessError("RealMoneyAuthorization is not currently usable")
        valid, reasons = verify_authorization_binding(
            approval, authorization, approval_sha256=approval_sha256,
            authorization_sha256=authorization_sha256, now_utc=now_utc,
        )
        if not valid:
            raise LiveReadinessError("RealMoneyAuthorization binding is invalid: " + "; ".join(reasons))
        digest = activation_phrase_digest(phrase, amount=authorization["exact_quote_amount_usdt"], client_order_id=str(authorization["client_order_id"]))
        now = now_utc.astimezone(UTC)
        auth_expires = datetime.fromisoformat(str(authorization["expires_at_utc"]).replace("Z", "+00:00"))
        expires = min(now + ACTIVATION_TTL, auth_expires)
        if expires <= now:
            raise LiveReadinessError("RealMoneyAuthorization is expired")
        body = {
            "schema_version": ACTIVATION_SCHEMA_VERSION,
            "created_at_utc": _utc_timestamp(now), "expires_at_utc": _utc_timestamp(expires),
            "status": "ACTIVE", "authorization_id": authorization["authorization_id"],
            "authorization_sha256": authorization_sha256,
            "approval_id": approval.payload["approval_id"], "approval_sha256": approval_sha256,
            "canary_manifest_id": approval.payload["canary_manifest_id"],
            "canary_manifest_sha256": approval.payload["canary_manifest_sha256"],
            "decision_id": approval.payload["decision_id"], "decision_sha256": approval.payload["decision_sha256"],
            "order_intent_id": approval.payload["order_intent_id"], "order_intent_sha256": approval.payload["order_intent_sha256"],
            "client_order_id": approval.payload["client_order_id"],
            "exact_quote_amount_usdt": approval.payload["quote_amount_usdt"],
            "calendar_month": approval.payload["calendar_month"],
            "repository_commit_sha": approval.payload["repository_commit_sha"],
            "execution_safety_config_fingerprint": approval.payload["execution_safety_config_fingerprint"],
            "production_contract_fingerprint": approval.payload["production_contract_fingerprint"],
            "account_identity_fingerprint": approval.payload["account_identity_fingerprint"],
            "one_shot_identity": approval.payload["one_shot_identity"],
            "activation_nonce_digest": digest, "standing_activation": False,
            "consumed": False, "no_order_executed": True,
        }
        body["activation_id"] = "activation-" + canonical_fingerprint({k: v for k, v in body.items() if k != "activation_id"})[:32]
        return cls(body)


def _activation_candidates(store: ArtifactStore) -> list[tuple[dict[str, Any], str]]:
    root = store.root / "execution_activations"
    rows: list[tuple[datetime, dict[str, Any], str]] = []
    for path in sorted(root.glob("*/*/*/*.json")):
        try:
            date = datetime(int(path.parts[-4]), int(path.parts[-3]), int(path.parts[-2]), tzinfo=UTC)
        except (TypeError, ValueError, IndexError) as exc:
            from .errors import ArtifactCorruptError
            raise ArtifactCorruptError("activation directory date is invalid") from exc
        payload = store.read(ArtifactType.EXECUTION_ACTIVATION, run_id=path.stem, artifact_date_utc=date)
        created = datetime.fromisoformat(str(payload["created_at_utc"]).replace("Z", "+00:00"))
        rows.append((created, payload, artifact_sha256(payload)))
    rows.sort(key=lambda row: row[0])
    return [(payload, digest) for _, payload, digest in rows]


def _submission_exists(store: ArtifactStore, payload: Mapping[str, Any]) -> bool:
    return store.has_submission_artifact(
        str(payload.get("decision_id")), str(payload.get("client_order_id")),
        kinds=(ArtifactType.LIVE_EXECUTION_ATTEMPT, ArtifactType.ORDER_SUBMISSION_ATTEMPT,
               ArtifactType.ORDER_SUBMISSION_OUTCOME),
    )


def create_execution_activation(
    *, store: ArtifactStore, authorization_id: str, authorization_sha256: str,
    approval_id: str, approval_sha256: str, client_order_id: str, amount: Any,
    calendar_month: str, phrase: str, run_id: str, now_utc: datetime,
) -> tuple[ExecutionActivation, Any]:
    authorization, persisted_auth_sha = store.find_artifact(
        ArtifactType.REAL_MONEY_AUTHORIZATION,
        identity_field="authorization_id", identity_value=authorization_id,
    )
    if persisted_auth_sha != authorization_sha256 or artifact_sha256(authorization) != authorization_sha256:
        raise LiveReadinessError("authorization SHA-256 does not match persisted canonical bytes")
    if authorization.get("approval_id") != approval_id or authorization.get("approval_sha256") != approval_sha256:
        raise LiveReadinessError("activation approval binding does not match authorization")
    if authorization.get("client_order_id") != client_order_id or _amount(authorization.get("exact_quote_amount_usdt")) != _amount(amount):
        raise LiveReadinessError("activation order identity does not match authorization")
    if authorization.get("calendar_month") != calendar_month:
        raise LiveReadinessError("activation month does not match authorization")
    approval_payload, persisted_approval_sha = store.find_artifact(
        ArtifactType.LIVE_EXECUTION_APPROVAL, identity_field="approval_id", identity_value=approval_id,
    )
    if persisted_approval_sha != approval_sha256:
        raise LiveReadinessError("approval SHA-256 does not match persisted artifact")
    approval = LiveExecutionApproval(approval_payload)
    if _submission_exists(store, authorization):
        raise LiveReadinessError("submission or activation evidence already exists for this identity")
    for prior, _ in _activation_candidates(store):
        if prior.get("one_shot_identity") == authorization.get("one_shot_identity"):
            try:
                expires = datetime.fromisoformat(str(prior["expires_at_utc"]).replace("Z", "+00:00"))
            except (KeyError, ValueError) as exc:
                raise LiveReadinessError("prior activation expiry is malformed") from exc
            # An unconsumed ACTIVE artifact stops being usable at expiry and
            # may be superseded append-only.  Consumed/revoked identities, or
            # any still-live activation, remain permanently non-refreshable.
            if prior.get("consumed") is True or prior.get("status") in {"CONSUMED", "REVOKED"} or now_utc.astimezone(UTC) < expires:
                raise LiveReadinessError("an existing usable or consumed ExecutionActivation exists for this identity")
    activation = ExecutionActivation.from_authorization(
        authorization, authorization_sha256=authorization_sha256, approval=approval,
        approval_sha256=approval_sha256, phrase=phrase, now_utc=now_utc,
    )
    return activation, activation.persist(store, run_id=run_id)


def _safe_runtime(config: ExecutionConfig | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(config, ExecutionConfig):
        return {"live_execution_enabled": config.live_execution_enabled, "kill_switch": config.kill_switch, "order_submission": config.order_submission}
    return {"live_execution_enabled": config.get("live_execution_enabled"), "kill_switch": config.get("kill_switch"), "order_submission": config.get("order_submission")}


def read_activation_status(*, store: ArtifactStore, activation_id: str | None = None, now_utc: datetime | None = None) -> dict[str, Any]:
    now = now_utc or datetime.now(UTC)
    if activation_id is None:
        candidates = _activation_candidates(store)
        if not candidates:
            return {"activation": "NOT_ACTIVATED", "authorization": "NOT_AUTHORIZED", "execution": "BLOCKED", "live_execution_enabled": False, "kill_switch": True, "order_submission": "not_implemented", "message": "NO ORDER EXECUTED"}
        payload, digest = candidates[-1]
    else:
        payload, digest = store.find_artifact(ArtifactType.EXECUTION_ACTIVATION, identity_field="activation_id", identity_value=activation_id)
    derived = str(payload.get("status", "INVALID"))
    if payload.get("status") == "REVOKED":
        derived = "REVOKED"
    elif _submission_exists(store, payload) or payload.get("consumed") is True:
        derived = "CONSUMED"
    else:
        try:
            expires = datetime.fromisoformat(str(payload["expires_at_utc"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            derived = "INVALID"
        else:
            if now.astimezone(UTC) >= expires:
                derived = "EXPIRED"
            elif derived not in {"ACTIVE", "EXPIRED", "CONSUMED", "REVOKED"}:
                derived = "INVALID"
    binding_valid = False
    try:
        authorization, authorization_sha = store.find_artifact(
            ArtifactType.REAL_MONEY_AUTHORIZATION,
            identity_field="authorization_id", identity_value=str(payload.get("authorization_id")),
        )
        approval, approval_sha = store.find_artifact(
            ArtifactType.LIVE_EXECUTION_APPROVAL,
            identity_field="approval_id", identity_value=str(payload.get("approval_id")),
        )
        binding_valid, _ = verify_authorization_binding(
            LiveExecutionApproval(approval), authorization,
            approval_sha256=approval_sha, authorization_sha256=authorization_sha,
            now_utc=now,
        )
        binding_valid = binding_valid and payload.get("authorization_sha256") == authorization_sha and payload.get("approval_sha256") == approval_sha
    except ArtifactNotFoundError:
        binding_valid = False
    config = _safe_runtime(load_execution_config())
    return {
        "activation": derived if derived in {"ACTIVE", "EXPIRED", "CONSUMED", "REVOKED"} else "INVALID",
        "authorization": "AUTHORIZED" if binding_valid and derived == "ACTIVE" else "NOT_AUTHORIZED",
        "activation_id": payload.get("activation_id"), "expires_at_utc": payload.get("expires_at_utc"),
        "consumed": derived == "CONSUMED", "authorization_binding": "VALID" if binding_valid else "INVALID",
        "one_shot_identity": payload.get("one_shot_identity"), "exact_quote_amount_usdt": payload.get("exact_quote_amount_usdt"),
        "client_order_id": payload.get("client_order_id"), "calendar_month": payload.get("calendar_month"),
        "execution": "READY" if derived == "ACTIVE" and config["live_execution_enabled"] and not config["kill_switch"] and config["order_submission"] in {"implemented", "implemented_disabled"} else "BLOCKED",
        **config, "message": "NO ORDER EXECUTED",
    }


class ExecutionCapability:
    """Non-serializable, identity-bound, one-shot runtime capability."""
    __slots__ = ("activation_id", "one_shot_identity", "request_fingerprint", "_seal", "_used")

    def __init__(self, *, activation_id: str, one_shot_identity: str, request_fingerprint: str, _internal: object):
        if not isinstance(_internal, _CAPABILITY_SEAL):
            raise LiveReadinessError("ExecutionCapability cannot be constructed directly")
        self.activation_id, self.one_shot_identity, self.request_fingerprint = activation_id, one_shot_identity, request_fingerprint
        self._seal, self._used = _internal, False

    @classmethod
    def for_test(cls, *, activation_id: str, one_shot_identity: str, request_fingerprint: str) -> "ExecutionCapability":
        """Create a fixture-only capability for fully mocked engine tests."""
        return cls(activation_id=activation_id, one_shot_identity=one_shot_identity, request_fingerprint=request_fingerprint, _internal=_CAPABILITY_SEAL())

    def assert_usable(self, *, activation_id: str, one_shot_identity: str, request_fingerprint: str) -> None:
        if self._used:
            raise LiveReadinessError("ExecutionCapability has already been consumed")
        if (self.activation_id, self.one_shot_identity, self.request_fingerprint) != (activation_id, one_shot_identity, request_fingerprint):
            raise LiveReadinessError("ExecutionCapability identity or request fingerprint changed")

    def consume(self) -> None:
        self._used = True


class _CAPABILITY_SEAL:
    pass


def issue_execution_capability(*, activation: Mapping[str, Any], activation_sha256: str, gate_a: GateResult, gate_b: GateResult, request_fingerprint: str, runtime_config: ExecutionConfig | Mapping[str, Any], now_utc: datetime) -> ExecutionCapability:
    if gate_a.status is not GateStatus.READY_FOR_SUBMISSION or gate_b.status is not GateStatus.READY_FOR_SUBMISSION:
        raise LiveReadinessError("Gate A and Gate B must pass before capability creation")
    if artifact_sha256(activation) != activation_sha256:
        raise LiveReadinessError("activation SHA-256 does not match canonical bytes")
    if activation.get("status") != "ACTIVE" or activation.get("consumed") is not False:
        raise LiveReadinessError("activation is not active")
    expires = datetime.fromisoformat(str(activation["expires_at_utc"]).replace("Z", "+00:00"))
    if now_utc.astimezone(UTC) >= expires:
        raise LiveReadinessError("activation is expired")
    safe = _safe_runtime(runtime_config)
    if safe["kill_switch"] is True:
        raise LiveReadinessError("kill switch is active")
    if safe["live_execution_enabled"] is not True or safe["order_submission"] not in {"implemented", "implemented_disabled"}:
        raise LiveReadinessError("runtime execution policy is not enabled")
    return ExecutionCapability(activation_id=str(activation["activation_id"]), one_shot_identity=str(activation["one_shot_identity"]), request_fingerprint=request_fingerprint, _internal=_CAPABILITY_SEAL())


def gate_c(*, capability: ExecutionCapability, activation: Mapping[str, Any], activation_sha256: str, request_fingerprint: str, runtime_config: ExecutionConfig | Mapping[str, Any], now_utc: datetime) -> None:
    capability.assert_usable(activation_id=str(activation["activation_id"]), one_shot_identity=str(activation["one_shot_identity"]), request_fingerprint=request_fingerprint)
    if artifact_sha256(activation) != activation_sha256:
        raise LiveReadinessError("Gate C activation bytes changed")
    expires = datetime.fromisoformat(str(activation["expires_at_utc"]).replace("Z", "+00:00"))
    if now_utc.astimezone(UTC) >= expires:
        raise LiveReadinessError("Gate C activation expired")
    safe = _safe_runtime(runtime_config)
    if safe["kill_switch"] is True:
        raise LiveReadinessError("Gate C kill switch is active")
    if safe["live_execution_enabled"] is not True or safe["order_submission"] not in {"implemented", "implemented_disabled"}:
        raise LiveReadinessError("Gate C runtime execution policy is blocked")
