"""Strict loader for repository-declared canonical resources."""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .errors import ConfigurationError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_SCHEMA = REPOSITORY_ROOT / "schemas" / "project_manifest.schema.json"


@dataclass(frozen=True)
class ProjectManifest:
    root: Path
    project_id: str
    strategy_id: str
    strategy_version: str
    strategy_config: Path
    strategy_content_sha256: str
    resources: Mapping[str, Path]

    def resource(self, name: str) -> Path:
        try:
            return self.resources[name]
        except KeyError as exc:
            raise ConfigurationError(f"project manifest has no resource {name!r}") from exc


def _resolve(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ConfigurationError(f"project manifest {label} must be a non-empty relative path")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ConfigurationError(f"project manifest {label} must not escape the repository")
    candidate = (root / Path(*path.parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigurationError(f"project manifest {label} resolves outside the repository") from exc
    if not candidate.is_file():
        raise ConfigurationError(f"project manifest {label} references a missing file: {relative}")
    return candidate


def load_project_manifest(
    *, root: Path = REPOSITORY_ROOT, manifest_path: Path | None = None
) -> ProjectManifest:
    resolved_root = Path(root).resolve()
    path = Path(manifest_path or resolved_root / "config" / "project_manifest.yaml").resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError as exc:
        raise ConfigurationError("project manifest path resolves outside the repository") from exc
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, yaml.YAMLError, json.JSONDecodeError, SchemaError) as exc:
        raise ConfigurationError(f"cannot load project manifest or schema: {exc}") from exc
    errors = sorted(Draft202012Validator(schema).iter_errors(raw), key=lambda item: list(item.path))
    if errors:
        error: ValidationError = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ConfigurationError(f"project manifest schema violation at {location}: {error.message}")
    strategy = raw["active_strategy"]
    strategy_path = _resolve(resolved_root, strategy["config_path"], "active_strategy.config_path")
    actual_strategy_sha256 = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
    if actual_strategy_sha256 != strategy["content_sha256"]:
        raise ConfigurationError(
            "active strategy config digest does not match the repository-approved manifest digest"
        )
    resources = {
        name: _resolve(resolved_root, value, f"canonical_resources.{name}")
        for name, value in raw["canonical_resources"].items()
    }
    if resources["ledger"].suffix != ".jsonl":
        raise ConfigurationError("canonical ledger resource must be JSONL")
    return ProjectManifest(
        resolved_root,
        str(raw["project"]["id"]),
        str(strategy["id"]),
        str(strategy["version"]),
        strategy_path,
        actual_strategy_sha256,
        resources,
    )
