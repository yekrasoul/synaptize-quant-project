"""Immutable sentiment-domain artifacts; no strategy calculations live here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SentimentSnapshot:
    schema_version: str
    snapshot_id: str
    source: str
    index_name: str
    value: int
    classification: str | None
    observed_at_utc: str
    retrieved_at_utc: str
    upstream_identifier: str
    value_in_range: bool = True
    fresh: bool = True
    source_identity_valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "index_name": self.index_name,
            "value": self.value,
            "classification": self.classification,
            "observed_at_utc": self.observed_at_utc,
            "retrieved_at_utc": self.retrieved_at_utc,
            "upstream_identifier": self.upstream_identifier,
            "validation": {
                "value_in_range": self.value_in_range,
                "fresh": self.fresh,
                "source_identity_valid": self.source_identity_valid,
            },
        }
