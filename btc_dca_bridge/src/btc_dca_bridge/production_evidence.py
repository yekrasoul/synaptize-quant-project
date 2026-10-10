"""Phase 5.7 immutable, read-only production evidence collection."""
from __future__ import annotations

import hashlib
import json
import platform
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from .artifacts import ArtifactStore
from .errors import ArtifactCorruptError, ArtifactNotFoundError
from .schemas import UnsupportedProductionEvidenceSchemaError
from .readiness import ProductionReadinessService, production_connectivity
from .quote_limits import PRODUCTION_QUOTE_UNIT_LIMIT_POLICY, QuoteUnitLimitEvidence, QuoteUnitLimitValidationError, unavailable_quote_unit_limit, validate_quote_unit_limit_evidence

EVIDENCE_TTL = timedelta(minutes=10)
DYNAMIC_TTLS_SECONDS = {"wallet_availability_liabilities": 60, "clock": 60, "account_metadata": 300, "instrument_metadata": 600}
EVIDENCE_STATES = {"EVIDENCE_COMPLETE_NOT_READY", "EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION", "EVIDENCE_INCOMPLETE"}
AUTHORIZATION = {"granted": False, "source": "none", "required": True, "status": "NOT_AUTHORIZED"}
_SENSITIVE_KEYS = {"secret", "apikey", "apisecret", "authorization", "signature", "bearer", "token", "xbapiapikey", "xbapisign", "telegrambottoken"}
_SENSITIVE_CONTAINERS = {"headers", "requestheaders", "authheaders"}


def _normalized_key(key: str) -> str:
    return "".join(character for character in key.lower() if character.isalnum())


def sanitize_evidence(value: Any, *, key: str = "") -> Any:
    """Remove secret-bearing keys recursively before persistence."""
    normalized = _normalized_key(key)
    if normalized in _SENSITIVE_KEYS:
        return None
    if normalized in _SENSITIVE_CONTAINERS:
        return {}
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            child_key = str(raw_key)
            child_normalized = _normalized_key(child_key)
            if child_normalized in _SENSITIVE_KEYS:
                continue
            if child_normalized in _SENSITIVE_CONTAINERS:
                sanitized[child_key] = {}
                continue
            child = sanitize_evidence(raw_value, key=child_key)
            if child is not None or raw_value is None:
                sanitized[child_key] = child
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_evidence(item) for item in value]
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("evidence clock must be UTC-aware")
    return value.astimezone(UTC)


def _check(checks: list[Mapping[str, Any]], check_id: str) -> dict[str, Any]:
    return next((dict(item) for item in checks if item.get("check_id") == check_id), {"check_id": check_id, "status": "UNAVAILABLE", "reason": "check not present"})


def _account_fingerprint(client: Any) -> tuple[str | None, str]:
    try:
        info = client.credential_info()
        identity = getattr(info, "identity", None)
        if not identity:
            return None, "ACCOUNT_IDENTITY_UNPROVEN"
        account = client.account_info()
        material = "|".join(("Bybit", str(account.unified_margin_status), str(account.margin_mode), str(account.spot_hedging_status), str(identity)))
        return hashlib.sha256(material.encode("utf-8")).hexdigest(), "PROVEN"
    except Exception:
        return None, "ACCOUNT_IDENTITY_UNPROVEN"


class ProductionEvidenceService:
    def __init__(self, *, data_root: Path, ledger_path: Path, now: Callable[[], datetime] | None = None, client_factory: Callable[[], Any] | None = None, readiness_factory: Callable[..., ProductionReadinessService] | None = None, connectivity_probe: Callable[[], Mapping[str, Any]] | None = None, repo_probe: Callable[[], Mapping[str, Any]] | None = None) -> None:
        self.data_root, self.ledger_path = Path(data_root), Path(ledger_path)
        self.now = now or (lambda: datetime.now(UTC))
        self.client_factory = client_factory
        self.readiness_factory = readiness_factory or ProductionReadinessService
        self.connectivity_probe = connectivity_probe
        self.repo_probe = repo_probe or ProductionReadinessService._repo_status

    def _readiness(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"data_root": self.data_root, "ledger_path": self.ledger_path, "now": self.now}
        if self.client_factory is not None:
            kwargs["client_factory"] = self.client_factory
        return self.readiness_factory(**kwargs).evaluate()

    def collect(self) -> tuple[dict[str, Any], Any]:
        created = _utc(self.now())
        readiness = self._readiness()
        connectivity = dict(self.connectivity_probe() if self.connectivity_probe else production_connectivity(client_factory=self.client_factory))
        connectivity["endpoints"] = [
            {
                **dict(endpoint),
                "reason": endpoint.get("reason", "GET read completed"),
                "observed_at_utc": endpoint.get("observed_at_utc", created.isoformat().replace("+00:00", "Z")),
                "response_contract_valid": endpoint.get("response_contract_valid", endpoint.get("status") == "PASS"),
            }
            for endpoint in connectivity.get("endpoints", [])
        ]
        repo = dict(self.repo_probe())
        client = None
        if self.client_factory is not None:
            try:
                client = self.client_factory()
            except Exception:
                client = None
        account_fingerprint, account_status = _account_fingerprint(client) if client is not None else (None, "ACCOUNT_IDENTITY_UNPROVEN")
        checks = [sanitize_evidence(item) for item in readiness.get("checks", [])]
        readiness_observations = readiness.get("observation_timestamps", {})
        if not isinstance(readiness_observations, Mapping):
            readiness_observations = {}

        created_iso = created.isoformat().replace("+00:00", "Z")

        def readiness_observed(name: str) -> str:
            value = readiness_observations.get(name)
            return str(value) if value else created_iso

        incomplete_ids = {"BYBIT_READ_ACCESS", "CLOCK_SKEW", "PRODUCTION_CONNECTIVITY", "SECRET_HYGIENE", "FILESYSTEM_DURABILITY", "OPERATOR_LOCK"}
        required_unavailable = any(item.get("required") and item.get("status") == "UNAVAILABLE" and item.get("check_id") in incomplete_ids for item in checks)
        connectivity_complete = connectivity.get("status") == "READS_OK"
        quote_limit_raw = QuoteUnitLimitEvidence.from_mapping(readiness.get("quote_unit_limit_evidence", unavailable_quote_unit_limit(observed_at_utc=created.isoformat().replace("+00:00", "Z"))))
        try:
            validate_quote_unit_limit_evidence(quote_limit_raw, now=created, policy=PRODUCTION_QUOTE_UNIT_LIMIT_POLICY)
            quote_limit_valid = True
        except QuoteUnitLimitValidationError:
            quote_limit_valid = False
        ready = readiness.get("status") == "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION" and connectivity_complete and account_status == "PROVEN" and quote_limit_valid
        status = "EVIDENCE_INCOMPLETE" if required_unavailable or not connectivity_complete else ("EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION" if ready else "EVIDENCE_COMPLETE_NOT_READY")
        evidence_id = "evidence-" + hashlib.sha256(f"{repo.get('commit')}|{socket.gethostname()}|{created.isoformat()}".encode()).hexdigest()[:32]
        by_id = {item.get("check_id"): item for item in checks}
        bundle: dict[str, Any] = {
            "schema_version": "5.8.0", "evidence_id": evidence_id,
            "created_at_utc": created.isoformat().replace("+00:00", "Z"),
            "expires_at_utc": (created + EVIDENCE_TTL).isoformat().replace("+00:00", "Z"),
            "repository_commit": str(repo.get("commit", "")), "hostname": socket.gethostname(), "pid": __import__("os").getpid(),
            "platform": platform.platform(), "python_version": platform.python_version(),
            "account_identity_fingerprint": account_fingerprint, "account_identity_status": account_status,
            "readiness_status": readiness.get("status", "NOT_READY"), "check_results": checks,
            "connectivity_result": sanitize_evidence(connectivity),
            "clock_result": sanitize_evidence(_check(checks, "CLOCK_SKEW")),
            "credential_scope_result": sanitize_evidence(_check(checks, "BYBIT_CREDENTIAL_SCOPE")),
            "account_mode_result": sanitize_evidence(_check(checks, "BYBIT_ACCOUNT")),
            "liability_result": sanitize_evidence(_check(checks, "BYBIT_LIABILITIES")),
            "instrument_result": sanitize_evidence(_check(checks, "BYBIT_INSTRUMENT")),
            "spot_availability_result": sanitize_evidence(_check(checks, "BYBIT_SPOT_AVAILABLE_BALANCE")),
            "quote_unit_limit_result": sanitize_evidence(quote_limit_raw.to_dict()),
            "monthly_budget_result": sanitize_evidence(_check(checks, "MONTHLY_BUDGET")),
            "unresolved_operations_result": sanitize_evidence(_check(checks, "UNRESOLVED_OPERATIONS")),
            "secret_hygiene_result": sanitize_evidence(_check(checks, "SECRET_HYGIENE")),
            "filesystem_result": sanitize_evidence(_check(checks, "FILESYSTEM_DURABILITY")),
            "operator_lock_result": sanitize_evidence(_check(checks, "OPERATOR_LOCK")),
            "real_money_authorization": dict(AUTHORIZATION), "status": status, "host_binding": "host-bound",
            "evidence_ttl_seconds": int(EVIDENCE_TTL.total_seconds()), "dynamic_ttls_seconds": DYNAMIC_TTLS_SECONDS,
            "wallet_observed_at_utc": readiness_observed("wallet"),
            "availability_observed_at_utc": readiness_observed("spot_quote_availability"),
            "liability_observed_at_utc": readiness_observed("wallet"),
            "clock_observed_at_utc": readiness_observed("clock"),
            "account_observed_at_utc": readiness_observed("account"),
            "instrument_observed_at_utc": readiness_observed("instrument"),
        }
        bundle = sanitize_evidence(bundle)
        receipt = ArtifactStore(self.data_root).persist_production_evidence(bundle)
        return bundle, receipt

    def verify(self, evidence_id: str, *, current_commit: str | None = None, current_hostname: str | None = None, current_account_fingerprint: str | None = None) -> dict[str, Any]:
        try:
            bundle = ArtifactStore(self.data_root).read_production_evidence(evidence_id)
        except ArtifactCorruptError as exc:
            return {"status": "CORRUPT", "evidence_id": evidence_id, "reason": str(exc)}
        except UnsupportedProductionEvidenceSchemaError as exc:
            return {"status": "UNSUPPORTED_SCHEMA_VERSION", "evidence_id": evidence_id, "reason": str(exc)}
        except ArtifactNotFoundError as exc:
            return {"status": "INVALID", "evidence_id": evidence_id, "reason": str(exc)}
        now = _utc(self.now())
        try:
            created = datetime.fromisoformat(bundle["created_at_utc"].replace("Z", "+00:00")).astimezone(UTC)
            expires = datetime.fromisoformat(bundle["expires_at_utc"].replace("Z", "+00:00")).astimezone(UTC)
        except (KeyError, TypeError, ValueError) as exc:
            return {"status": "INVALID", "evidence_id": evidence_id, "reason": f"invalid evidence timestamps: {exc}"}
        if created > now or expires <= now:
            return {"status": "EXPIRED" if expires <= now else "INVALID", "evidence_id": evidence_id, "reason": "evidence TTL is not currently valid"}
        if expires - created > EVIDENCE_TTL or bundle.get("real_money_authorization") != AUTHORIZATION:
            return {"status": "INVALID", "evidence_id": evidence_id, "reason": "evidence TTL or authorization boundary is invalid"}
        dynamic_fields = {
            "wallet_observed_at_utc": DYNAMIC_TTLS_SECONDS["wallet_availability_liabilities"],
            "availability_observed_at_utc": DYNAMIC_TTLS_SECONDS["wallet_availability_liabilities"],
            "liability_observed_at_utc": DYNAMIC_TTLS_SECONDS["wallet_availability_liabilities"],
            "clock_observed_at_utc": DYNAMIC_TTLS_SECONDS["clock"],
            "account_observed_at_utc": DYNAMIC_TTLS_SECONDS["account_metadata"],
            "instrument_observed_at_utc": DYNAMIC_TTLS_SECONDS["instrument_metadata"],
        }
        for field, ttl_seconds in dynamic_fields.items():
            try:
                observed = datetime.fromisoformat(str(bundle[field]).replace("Z", "+00:00")).astimezone(UTC)
            except (KeyError, TypeError, ValueError) as exc:
                return {"status": "INVALID", "evidence_id": evidence_id, "reason": f"invalid {field}: {exc}"}
            age = (now - observed).total_seconds()
            if age < 0:
                return {"status": "INVALID", "evidence_id": evidence_id, "reason": f"{field} is future-dated"}
            if age > ttl_seconds:
                return {"status": "STALE", "evidence_id": evidence_id, "reason": f"{field} is older than {ttl_seconds}s"}
        commit = current_commit or self.repo_probe().get("commit")
        hostname = current_hostname or socket.gethostname()
        if bundle.get("repository_commit") != commit:
            return {"status": "INVALID_FOR_CURRENT_BUILD", "evidence_id": evidence_id, "reason": "repository commit changed after collection"}
        if bundle.get("host_binding") == "host-bound" and bundle.get("hostname") != hostname:
            return {"status": "INVALID", "evidence_id": evidence_id, "reason": "host-bound evidence belongs to another host"}
        if bundle.get("account_identity_status") == "PROVEN":
            if current_account_fingerprint is None and self.client_factory is not None:
                try:
                    current_account_fingerprint, _ = _account_fingerprint(self.client_factory())
                except Exception:
                    current_account_fingerprint = None
            if current_account_fingerprint is None:
                return {"status": "INVALID", "evidence_id": evidence_id, "reason": "ACCOUNT_IDENTITY_UNAVAILABLE: current fingerprint is required"}
            if bundle.get("account_identity_fingerprint") != current_account_fingerprint:
                return {"status": "INVALID", "evidence_id": evidence_id, "reason": "account identity fingerprint mismatch"}
        if bundle.get("schema_version") == "5.7.0":
            return {
                "status": "VALID_NOT_READY",
                "evidence_id": evidence_id,
                "reason": "legacy evidence predates Phase 5.8 quote-unit-limit proof",
                "expires_at_utc": bundle["expires_at_utc"],
                "real_money_authorization": dict(AUTHORIZATION),
            }
        ready = bundle.get("status") == "EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION" and bundle.get("account_identity_status") == "PROVEN"
        return {"status": "VALID_READY_FOR_SEPARATE_AUTHORIZATION" if ready else "VALID_NOT_READY", "evidence_id": evidence_id, "expires_at_utc": bundle["expires_at_utc"], "real_money_authorization": dict(AUTHORIZATION)}

    def latest(self) -> dict[str, Any] | None:
        root = self.data_root / "production_evidence"
        paths = sorted(path for path in root.glob("*/*/*/*.json") if path.suffix == ".json")
        if not paths:
            return None
        store = ArtifactStore(self.data_root)
        bundles = [store.read_production_evidence(path.stem) for path in paths]
        return max(bundles, key=lambda bundle: (datetime.fromisoformat(str(bundle["created_at_utc"]).replace("Z", "+00:00")).astimezone(UTC), str(bundle["evidence_id"])))


def preauthorization_status(service: ProductionEvidenceService) -> dict[str, Any]:
    latest = service.latest()
    if latest is None:
        return {"status": "BLOCKED", "reason": "no production evidence bundle exists", "allowed_actions": ["collect-production-evidence"], "real_money_authorization": dict(AUTHORIZATION)}
    verification = service.verify(str(latest["evidence_id"]))
    if verification["status"] not in {"VALID_NOT_READY", "VALID_READY_FOR_SEPARATE_AUTHORIZATION"}:
        return {"status": "BLOCKED", "reason": verification.get("reason", verification["status"]), "evidence_id": latest["evidence_id"], "real_money_authorization": dict(AUTHORIZATION)}
    readiness = service._readiness()
    state = readiness.get("status")
    quote_limit = QuoteUnitLimitEvidence.from_mapping(readiness.get("quote_unit_limit_evidence", unavailable_quote_unit_limit(observed_at_utc=service.now().astimezone(UTC).isoformat().replace("+00:00", "Z"))))
    try:
        validate_quote_unit_limit_evidence(quote_limit, now=service.now(), policy=PRODUCTION_QUOTE_UNIT_LIMIT_POLICY)
        quote_limit_status = "PASS"
        quote_limit_reason = "authoritative quote-unit maximum is valid"
    except QuoteUnitLimitValidationError as exc:
        quote_limit_status = "UNAVAILABLE"
        quote_limit_reason = str(exc)
        state = "NOT_READY"
    mapped = "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION" if state == "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION" and verification["status"] == "VALID_READY_FOR_SEPARATE_AUTHORIZATION" else ("READY_FOR_OPERATOR_PREPARATION" if state == "READY_FOR_OPERATOR_PREPARATION" else "BLOCKED")
    return {"status": mapped, "evidence_id": latest["evidence_id"], "readiness_status": state, "quote_unit_limit": {"status": quote_limit_status, "reason": quote_limit_reason, "evidence": quote_limit.to_dict() if hasattr(quote_limit, "to_dict") else quote_limit}, "verification": verification, "real_money_authorization": dict(AUTHORIZATION)}
