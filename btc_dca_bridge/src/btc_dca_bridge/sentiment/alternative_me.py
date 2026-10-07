"""Read-only adapter for Alternative.me's public Crypto Fear & Greed API."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Callable, Protocol

from ..errors import (
    InvalidSentimentResponseError,
    InvalidSentimentTimestampError,
    InvalidSentimentValueError,
    SentimentDataStaleError,
    SentimentRateLimitedError,
    SentimentSourceUnavailableError,
)
from ..market_data.http import HttpResponse, PublicHttpTransport, TransportConnectionError, TransportTimeout
from ..schemas import validate_artifact
from .models import SentimentSnapshot


ALTERNATIVE_ME_BASE_URL = "https://api.alternative.me"
FEAR_GREED_PATH = "/fng/"
SOURCE = "alternative_me_crypto_fear_greed"
INDEX_NAME = "Crypto Fear & Greed Index"
UPSTREAM_INDEX_NAME = "Fear and Greed Index"
UPSTREAM_IDENTIFIER = "https://api.alternative.me/fng/?limit=1&format=json"
# The upstream index is published daily.  36 hours permits ordinary publication
# variation while rejecting a missed daily observation; it is deliberately not
# the five-minute BTC price policy.
DEFAULT_MAX_OBSERVATION_AGE = timedelta(hours=36)


class HttpGetter(Protocol):
    def get(self, path: str, params: dict[str, str | int]) -> HttpResponse: ...


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise InvalidSentimentTimestampError(f"{label} must be a UTC-aware datetime", source=SOURCE)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InvalidSentimentTimestampError("source timestamp must be integral Unix seconds", source=SOURCE)
    text = str(value)
    if not text.isdigit():
        raise InvalidSentimentTimestampError("source timestamp must be integral Unix seconds", source=SOURCE)
    try:
        return datetime.fromtimestamp(int(text), UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidSentimentTimestampError("source timestamp is impossible", source=SOURCE) from exc


def _value(value: object) -> int:
    # The upstream contract serializes its integer index as a decimal string.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InvalidSentimentValueError("value must be an integer", source=SOURCE)
    text = str(value)
    if not text.isdigit():
        raise InvalidSentimentValueError("value must be an integer", source=SOURCE)
    result = int(text)
    if not 0 <= result <= 100:
        raise InvalidSentimentValueError("value must be within 0..100", source=SOURCE)
    return result


class AlternativeMeFearGreedAdapter:
    """Retrieve exactly one current Alternative.me reading and fail closed."""

    def __init__(
        self,
        *,
        transport: HttpGetter | None = None,
        clock: Callable[[], datetime] | None = None,
        max_observation_age: timedelta = DEFAULT_MAX_OBSERVATION_AGE,
    ) -> None:
        if not isinstance(max_observation_age, timedelta) or max_observation_age <= timedelta(0):
            raise ValueError("max_observation_age must be greater than zero")
        self._transport = transport or PublicHttpTransport(ALTERNATIVE_ME_BASE_URL)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.max_observation_age = max_observation_age

    def fetch_current(self) -> SentimentSnapshot:
        try:
            response = self._transport.get(FEAR_GREED_PATH, {"limit": 1, "format": "json"})
        except (TransportTimeout, TransportConnectionError) as exc:
            raise SentimentSourceUnavailableError(str(exc), retryable=True, source=SOURCE) from exc
        if response.status == 429:
            raise SentimentRateLimitedError("Alternative.me rate limited the request", status_code=429, retryable=True, source=SOURCE)
        if not 200 <= response.status < 300:
            raise SentimentSourceUnavailableError("Alternative.me returned an unavailable HTTP status", status_code=response.status, retryable=response.status >= 500, source=SOURCE)
        retrieved = _utc(self._clock(), "retrieval time")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidSentimentResponseError("response is not valid JSON", source=SOURCE) from exc
        if not isinstance(payload, dict) or payload.get("name") != UPSTREAM_INDEX_NAME:
            raise InvalidSentimentResponseError("response lacks the expected index identity", source=SOURCE)
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("error") is not None:
            raise InvalidSentimentResponseError("response reports an upstream error", source=SOURCE)
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise InvalidSentimentResponseError("response must contain exactly one current reading", source=SOURCE)
        row = data[0]
        observed = _timestamp(row.get("timestamp"))
        if observed > retrieved:
            raise InvalidSentimentTimestampError("source timestamp is in the future", source=SOURCE)
        if retrieved - observed > self.max_observation_age:
            raise SentimentDataStaleError("source observation exceeds configured sentiment freshness policy", source=SOURCE)
        value = _value(row.get("value"))
        classification = row.get("value_classification")
        if classification is not None and (not isinstance(classification, str) or not classification.strip()):
            raise InvalidSentimentResponseError("classification must be a non-empty string when supplied", source=SOURCE)
        snapshot_id = "sentiment_" + hashlib.sha256(
            f"{SOURCE}|{observed.timestamp():.0f}|{value}|{classification or ''}".encode("utf-8")
        ).hexdigest()[:24]
        snapshot = SentimentSnapshot(
            schema_version="1.0.0", snapshot_id=snapshot_id, source=SOURCE,
            index_name=INDEX_NAME, value=value, classification=classification,
            observed_at_utc=_iso(observed), retrieved_at_utc=_iso(retrieved),
            upstream_identifier=UPSTREAM_IDENTIFIER,
        )
        validate_artifact("sentiment_snapshot", snapshot.to_dict())
        return snapshot
