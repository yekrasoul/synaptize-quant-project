"""Small operational venue circuit breaker; distinct from the kill switch."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class VenueHealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class VenueHealthEvidence:
    state: VenueHealthState
    consecutive_failures: int
    reason: str | None = None


class VenueCircuitBreaker:
    """Fail closed after bounded failed probes; a successful probe recovers."""
    def __init__(self, *, failure_threshold: int = 3) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        self.failure_threshold = failure_threshold
        # State is deliberately not persisted. A restart starts open until a
        # successful fresh probe supplies new evidence.
        self._failures = failure_threshold
        self._reason: str | None = "startup health probe required"

    def record_failure(self, reason: str) -> VenueHealthEvidence:
        self._failures += 1
        self._reason = reason
        return self.current()

    def record_success(self) -> VenueHealthEvidence:
        self._failures = 0
        self._reason = None
        return self.current()

    def current(self) -> VenueHealthEvidence:
        if self._failures >= self.failure_threshold:
            state = VenueHealthState.UNAVAILABLE
        elif self._failures:
            state = VenueHealthState.DEGRADED
        else:
            state = VenueHealthState.HEALTHY
        return VenueHealthEvidence(state, self._failures, self._reason)
