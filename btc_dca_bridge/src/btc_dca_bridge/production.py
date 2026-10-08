"""Production-shadow run context, structured outcomes, and notification handoff."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from .artifacts import make_run_id
from .config import RuntimeConfig, load_operational_config, load_runtime_config, load_strategy_config
from .errors import BtcDcaError, ShadowRunAlreadyCompletedError, ShadowRunError
from .ledger import read_executions
from .paths import CONFIG_PATH, DATA_PATH, LEDGER_PATH
from .schemas import validate_all_schemas
from .shadow import ShadowPipeline, build_live_shadow_pipeline


TRIGGERS = {"scheduled", "manual"}


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be a UTC-aware datetime")
    return value.astimezone(UTC)


def parse_utc(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be an RFC 3339 UTC timestamp") from exc
    return _utc(parsed, label)


def iso_utc(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def scheduled_slot(
    process_started_at_utc: datetime, *, runtime_config: RuntimeConfig | None = None
) -> datetime:
    """Return the latest validated configured UTC slot at process start."""
    started = _utc(process_started_at_utc, "process_started_at_utc")
    runtime = runtime_config or load_runtime_config()
    anchor = started.replace(
        hour=runtime.scheduled_utc_hour,
        minute=runtime.scheduled_utc_minute,
        second=0,
        microsecond=0,
    )
    if anchor > started:
        anchor -= timedelta(days=1)
    elapsed_hours = int((started - anchor).total_seconds() // 3600)
    steps = elapsed_hours // runtime.scheduled_interval_hours
    return anchor + timedelta(hours=steps * runtime.scheduled_interval_hours)


def acquisition_minute(now_utc: datetime) -> datetime:
    """Select now when aligned, otherwise the next safe minute boundary."""
    now = _utc(now_utc, "acquisition clock")
    aligned = now.replace(second=0, microsecond=0)
    return aligned if now == aligned else aligned + timedelta(minutes=1)


def scheduled_run_id(slot_utc: datetime, *, runtime_config: RuntimeConfig | None = None) -> str:
    slot = _utc(slot_utc, "scheduled slot")
    runtime = runtime_config or load_runtime_config()
    if slot.second or slot.microsecond or slot.minute != runtime.scheduled_utc_minute or slot.hour != runtime.scheduled_utc_hour:
        raise ValueError("scheduled slot must be aligned to the configured UTC minute")
    digest = hashlib.sha256(f"scheduled|{iso_utc(slot)}".encode("utf-8")).hexdigest()[:12]
    return make_run_id(slot, f"scheduled_{digest}")


def manual_run_id(logical_at_utc: datetime, trigger_id: str) -> str:
    logical = _utc(logical_at_utc, "manual logical time")
    if logical.second or logical.microsecond:
        raise ValueError("manual logical time must be minute-aligned")
    if not isinstance(trigger_id, str) or not trigger_id.strip():
        raise ValueError("manual trigger ID must be non-empty")
    digest = hashlib.sha256(
        f"manual|{iso_utc(logical)}|{trigger_id}".encode("utf-8")
    ).hexdigest()[:12]
    return make_run_id(logical, f"manual_{digest}")


@dataclass(frozen=True)
class ProductionRunContext:
    trigger_type: str
    logical_run_at_utc: str
    process_started_at_utc: str
    run_id: str
    schema_version: str = "1.0.0"

    @classmethod
    def create(
        cls,
        *,
        trigger_type: str,
        process_started_at_utc: datetime,
        trigger_id: str,
        trigger_created_at_utc: datetime | None = None,
        runtime_config: RuntimeConfig | None = None,
    ) -> "ProductionRunContext":
        if trigger_type not in TRIGGERS:
            raise ValueError("trigger_type must be scheduled or manual")
        started = _utc(process_started_at_utc, "process_started_at_utc")
        runtime = runtime_config or load_runtime_config()
        if trigger_type == "scheduled":
            logical = scheduled_slot(started, runtime_config=runtime)
            run_id = scheduled_run_id(logical, runtime_config=runtime)
        else:
            created = _utc(
                trigger_created_at_utc or started, "trigger_created_at_utc"
            )
            logical = created.replace(second=0, microsecond=0)
            run_id = manual_run_id(logical, trigger_id)
        return cls(trigger_type, iso_utc(logical), iso_utc(started), run_id)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProductionRunContext":
        context = cls(
            trigger_type=str(value.get("trigger_type", "")),
            logical_run_at_utc=str(value.get("logical_run_at_utc", "")),
            process_started_at_utc=str(value.get("process_started_at_utc", "")),
            run_id=str(value.get("run_id", "")),
            schema_version=str(value.get("schema_version", "")),
        )
        if context.schema_version != "1.0.0" or context.trigger_type not in TRIGGERS:
            raise ValueError("unsupported production run context")
        logical = parse_utc(context.logical_run_at_utc, "logical_run_at_utc")
        parse_utc(context.process_started_at_utc, "process_started_at_utc")
        expected = (
            scheduled_run_id(logical)
            if context.trigger_type == "scheduled"
            else context.run_id
        )
        if context.trigger_type == "scheduled" and context.run_id != expected:
            raise ValueError("scheduled context run_id does not match logical slot")
        # make_run_id validates the complete path-safe shape for both trigger types.
        suffix = context.run_id.split("_", 2)[-1]
        if make_run_id(logical, suffix) != context.run_id:
            raise ValueError("context run_id does not match logical time")
        return context

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "trigger_type": self.trigger_type,
            "logical_run_at_utc": self.logical_run_at_utc,
            "process_started_at_utc": self.process_started_at_utc,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class ProductionShadowResult:
    payload: dict[str, Any]

    @property
    def status(self) -> str:
        return str(self.payload["status"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


PipelineFactory = Callable[..., ShadowPipeline]


def run_production_shadow(
    context: ProductionRunContext,
    *,
    config_path: Path = CONFIG_PATH,
    ledger_path: Path = LEDGER_PATH,
    data_root: Path | None = None,
    pipeline_factory: PipelineFactory = build_live_shadow_pipeline,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> ProductionShadowResult:
    """Run the canonical pipeline once and return a structured operational result."""
    now = _utc(clock(), "production clock")
    acquisition_at = acquisition_minute(now)
    wait_seconds = (acquisition_at - now).total_seconds()
    base: dict[str, Any] = {
        **context.to_dict(),
        "acquisition_at_utc": iso_utc(acquisition_at),
        "mode": "shadow",
        "no_order_executed": True,
        "notification_status": "pending",
    }
    try:
        # Fail before public network acquisition if repository-owned inputs are invalid.
        validate_all_schemas()
        load_operational_config()
        load_strategy_config(config_path)
        read_executions(ledger_path)
        if wait_seconds > 0:
            sleep(wait_seconds)
        pipeline = pipeline_factory(
            run_at_utc=acquisition_at,
            config_path=config_path,
            ledger_path=ledger_path,
            data_root=data_root,
        )
        shadow = pipeline.run(
            run_at_utc=acquisition_at,
            run_id=context.run_id,
            run_identity_at_utc=parse_utc(
                context.logical_run_at_utc, "logical_run_at_utc"
            ),
        )
    except ShadowRunAlreadyCompletedError:
        base.update(
            status="already_completed",
            notification_status="suppressed",
            duplicate_notification_suppressed=True,
        )
        return ProductionShadowResult(base)
    except ShadowRunError as exc:
        base.update(status="failed", failure=_failure(exc))
        return ProductionShadowResult(base)
    except (BtcDcaError, ValueError) as exc:
        base.update(
            status="failed",
            failure={
                "stage": "PREFLIGHT",
                "category": "PREFLIGHT_FAILED",
                "source": None,
                "message": str(exc),
            },
        )
        return ProductionShadowResult(base)
    base.update(status="completed", shadow_result=shadow.to_dict())
    return ProductionShadowResult(base)


def _failure(error: ShadowRunError) -> dict[str, Any]:
    cause = error.cause
    source = getattr(cause, "source", None)
    category = getattr(getattr(cause, "code", None), "value", None) or error.code.value
    return {
        "stage": error.stage,
        "category": category,
        "source": source,
        "message": str(error),
    }


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read structured JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("structured JSON root must be an object")
    return value


def write_json_object(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically publish ephemeral workflow handoff JSON (not a canonical artifact)."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
