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
    "sentiment_snapshot": "sentiment_snapshot.schema.json",
    "shadow_run": "shadow_run.schema.json",
    "order_intent": "order_intent.schema.json",
    "safety_validation": "safety_validation.schema.json",
    "order_submission": "order_submission.schema.json",
    "reconciliation_result": "reconciliation_result.schema.json",
}


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
    return sorted(SCHEMA_FILES)
