"""Small correlation envelope, deliberately not a full event-sourcing system."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    event_type: str
    occurred_at: str
    observed_at: str
    correlation_id: str
    aggregate_id: str
    schema_version: str
    payload: Mapping[str, Any]
    causation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
