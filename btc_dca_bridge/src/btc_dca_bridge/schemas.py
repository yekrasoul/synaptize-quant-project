"""JSON Schema loading and validation at system boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from .errors import SchemaValidationError
from .paths import SCHEMAS_PATH


SCHEMA_FILES = {
    "decision": "decision.schema.json",
    "execution": "execution.schema.json",
    "market_snapshot": "market_snapshot.schema.json",
    "notification_event": "notification_event.schema.json",
    "portfolio_state": "portfolio_state.schema.json",
    "portfolio_state_1_0": "portfolio_state_v1_0.schema.json",
    "portfolio_state_1_1": "portfolio_state_v1_1.schema.json",
    "sentiment_snapshot": "sentiment_snapshot.schema.json",
    "shadow_run": "shadow_run.schema.json",
    "order_intent": "order_intent.schema.json",
    "safety_validation": "safety_validation.schema.json",
    "order_submission_attempt": "order_submission_attempt.schema.json",
    "order_submission_outcome": "order_submission_outcome.schema.json",
    "canary_manifest": "canary_manifest.schema.json",
    "order_submission": "order_submission.schema.json",
    "reconciliation_result": "reconciliation_result.schema.json",
    "submission_reconciliation": "submission_reconciliation.schema.json",
    "production_evidence": "production_evidence.schema.json",
    "production_status": "production_status.schema.json",
    "production_status_alert": "production_status_alert.schema.json",
    "project_manifest": "project_manifest.schema.json",
}


class UnsupportedProductionEvidenceSchemaError(SchemaValidationError):
    """The bytes identify a schema version this build does not understand."""

PRODUCTION_EVIDENCE_SCHEMA_FILES = {
    "5.7.0": "production_evidence_v5_7.schema.json",
    "5.8.0": "production_evidence.schema.json",
}

def validate_live_approval(artifact: dict[str, Any], schemas_path: Path = SCHEMAS_PATH) -> None:
    """Validate the approval artifact without expanding the legacy schema set."""
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": False, "required": ["schema_version", "approval_id", "canary_id", "manifest_sha256", "decision_id", "order_intent_id", "client_order_id", "approved_amount_usdt", "order_payload_fingerprint", "exchange", "market_type", "symbol", "side", "order_type", "approved_at_utc", "expires_at_utc", "standing_authorization"], "properties": {"schema_version": {"const": "5.4.0"}, "approval_id": {"type": "string", "pattern": "^approval-[a-f0-9]{32}$"}, "canary_id": {"type": "string", "pattern": "^canary-[a-f0-9]{32}$"}, "manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "decision_id": {"type": "string"}, "order_intent_id": {"type": "string"}, "client_order_id": {"type": "string", "pattern": "^dca-[a-f0-9]{32}$"}, "approved_amount_usdt": {"type": "string"}, "order_payload_fingerprint": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "exchange": {"const": "Bybit"}, "market_type": {"const": "spot"}, "symbol": {"const": "BTCUSDT"}, "side": {"const": "Buy"}, "order_type": {"const": "Market"}, "approved_at_utc": {"type": "string", "format": "date-time"}, "expires_at_utc": {"type": "string", "format": "date-time"}, "standing_authorization": {"const": False}}}
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(artifact), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise SchemaValidationError(f"live_approval schema violation at {location}: {error.message}")


def load_schema(name: str, schemas_path: Path = SCHEMAS_PATH) -> dict[str, Any]:
    try:
        filename = SCHEMA_FILES[name]
    except KeyError as exc:
        raise SchemaValidationError(f"unknown schema: {name}") from exc
    path = schemas_path / filename
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        raise SchemaValidationError(f"invalid schema {path}: {exc}") from exc
    return schema


def validate_artifact(
    name: str, artifact: dict[str, Any], schemas_path: Path = SCHEMAS_PATH
) -> None:
    schema_name = name
    if name == "portfolio_state" and artifact.get("schema_version") == "1.0.0":
        schema_name = "portfolio_state_1_0"
    elif name == "portfolio_state" and artifact.get("schema_version") == "1.1.0":
        schema_name = "portfolio_state_1_1"
    if name == "production_evidence":
        version = artifact.get("schema_version")
        filename = PRODUCTION_EVIDENCE_SCHEMA_FILES.get(version)
        if filename is None:
            raise UnsupportedProductionEvidenceSchemaError(f"unsupported production evidence schema version: {version}")
        schema = _load_schema_file(filename, schemas_path)
    else:
        schema = load_schema(schema_name, schemas_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(artifact), key=lambda error: list(error.path))
    if errors:
        error: ValidationError = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise SchemaValidationError(f"{name} schema violation at {location}: {error.message}")


def validate_all_schemas(schemas_path: Path = SCHEMAS_PATH) -> list[str]:
    for name in SCHEMA_FILES:
        load_schema(name, schemas_path)
    for filename in PRODUCTION_EVIDENCE_SCHEMA_FILES.values():
        _load_schema_file(filename, schemas_path)
    return sorted(SCHEMA_FILES)


def _load_schema_file(filename: str, schemas_path: Path) -> dict[str, Any]:
    path = schemas_path / filename
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        raise SchemaValidationError(f"invalid schema {path}: {exc}") from exc
    return schema
