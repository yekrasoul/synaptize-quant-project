"""Domain-specific failures for the BTC DCA core and read-only adapters."""

from __future__ import annotations

from enum import Enum


class BtcDcaError(ValueError):
    """Base class for invalid offline inputs or canonical artifacts."""


class ConfigurationError(BtcDcaError):
    """The strategy configuration is malformed or unsupported."""


class InputValidationError(BtcDcaError):
    """A calculation input is invalid or contradictory."""


class LedgerValidationError(BtcDcaError):
    """The canonical execution ledger cannot be safely interpreted."""


class AmbiguousManualExecutionError(LedgerValidationError):
    """A manual report may be a duplicate but identity evidence is not exact."""


class SchemaValidationError(BtcDcaError):
    """An artifact does not conform to its canonical JSON Schema."""


class MarketDataErrorCode(str, Enum):
    """Stable machine-readable failure categories for market-data callers."""

    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    # Kept as a named legacy category for callers reading older artifacts.
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    INVALID_MARKET_IDENTITY = "INVALID_MARKET_IDENTITY"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    DATA_STALE = "DATA_STALE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_WINDOW = "INVALID_WINDOW"
    CONTRADICTORY_PRICE_DATA = "CONTRADICTORY_PRICE_DATA"


class MarketDataError(BtcDcaError):
    """Base failure raised by a market-data source or normalization boundary."""

    default_code = MarketDataErrorCode.SOURCE_UNAVAILABLE

    def __init__(
        self,
        message: str,
        *,
        code: MarketDataErrorCode | None = None,
        status_code: int | None = None,
        retryable: bool = False,
        source: str | None = None,
    ) -> None:
        self.code = code or self.default_code
        self.status_code = status_code
        self.retryable = retryable
        self.source = source
        super().__init__(f"{self.code.value}: {message}")


class DataUnavailableError(MarketDataError):
    """The configured source could not supply usable data."""


class SourceMismatchError(MarketDataError):
    """The payload identifies a different symbol or retrieval source."""

    default_code = MarketDataErrorCode.SOURCE_MISMATCH


class InvalidMarketIdentityError(MarketDataError):
    """The payload does not prove the required Spot market identity."""

    default_code = MarketDataErrorCode.INVALID_MARKET_IDENTITY


class InvalidResponseError(MarketDataError):
    """The source response is malformed, contradictory, or logically invalid."""

    default_code = MarketDataErrorCode.INVALID_RESPONSE


class DataStaleError(MarketDataError):
    """The source data is older than a caller's accepted freshness policy."""

    default_code = MarketDataErrorCode.DATA_STALE


class InsufficientHistoryError(MarketDataError):
    """Candle retrieval cannot prove complete requested interval coverage."""

    default_code = MarketDataErrorCode.INSUFFICIENT_HISTORY


class RateLimitedError(MarketDataError):
    """A public source rejected a request because of request frequency."""

    default_code = MarketDataErrorCode.RATE_LIMITED


class InvalidWindowError(InsufficientHistoryError):
    """The source resolution cannot prove the exact requested time window."""

    default_code = MarketDataErrorCode.INVALID_WINDOW


class ContradictoryPriceDataError(InvalidResponseError):
    """Prices within one source path contradict one another."""

    default_code = MarketDataErrorCode.CONTRADICTORY_PRICE_DATA


class AllSourcesUnavailableError(DataUnavailableError):
    """Every configured complete source path failed with an availability error."""

    def __init__(
        self,
        message: str,
        *,
        primary_source: str,
        primary_failure: MarketDataError,
        fallback_source: str,
        fallback_failure: MarketDataError,
    ) -> None:
        self.primary_source = primary_source
        self.primary_failure = primary_failure
        self.fallback_source = fallback_source
        self.fallback_failure = fallback_failure
        self.fallback_attempted = True
        super().__init__(message, source=fallback_source)


class SentimentErrorCode(str, Enum):
    """Stable machine-readable failure categories for sentiment callers."""

    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    INVALID_SENTIMENT_VALUE = "INVALID_SENTIMENT_VALUE"
    DATA_STALE = "DATA_STALE"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"


class SentimentError(BtcDcaError):
    """Base failure raised by the independent sentiment boundary."""

    default_code = SentimentErrorCode.SOURCE_UNAVAILABLE

    def __init__(
        self,
        message: str,
        *,
        code: SentimentErrorCode | None = None,
        status_code: int | None = None,
        retryable: bool = False,
        source: str | None = None,
    ) -> None:
        self.code = code or self.default_code
        self.status_code = status_code
        self.retryable = retryable
        self.source = source
        super().__init__(f"{self.code.value}: {message}")


class SentimentSourceUnavailableError(SentimentError):
    """The configured source could not provide a usable reading."""


class InvalidSentimentResponseError(SentimentError):
    """The source response lacks required provenance or has malformed JSON."""

    default_code = SentimentErrorCode.INVALID_RESPONSE


class InvalidSentimentValueError(SentimentError):
    """The source value is not an integer in the V1 range 0..100."""

    default_code = SentimentErrorCode.INVALID_SENTIMENT_VALUE


class SentimentDataStaleError(SentimentError):
    """The source observation exceeds the configured sentiment age."""

    default_code = SentimentErrorCode.DATA_STALE


class InvalidSentimentTimestampError(SentimentError):
    """The source observation timestamp is malformed or impossible."""

    default_code = SentimentErrorCode.INVALID_TIMESTAMP


class SentimentRateLimitedError(SentimentError):
    """The public sentiment source rejected the request due to rate limits."""

    default_code = SentimentErrorCode.RATE_LIMITED


class ArtifactErrorCode(str, Enum):
    """Stable machine-readable failure categories for artifact persistence."""

    ARTIFACT_ALREADY_EXISTS = "ARTIFACT_ALREADY_EXISTS"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    ARTIFACT_CORRUPT = "ARTIFACT_CORRUPT"
    PERSISTENCE_IO_ERROR = "PERSISTENCE_IO_ERROR"
    INVALID_ARTIFACT_PATH = "INVALID_ARTIFACT_PATH"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"


class ArtifactError(BtcDcaError):
    """Base failure at the immutable artifact filesystem boundary."""

    default_code = ArtifactErrorCode.ARTIFACT_INVALID

    def __init__(self, message: str, *, code: ArtifactErrorCode | None = None) -> None:
        self.code = code or self.default_code
        super().__init__(f"{self.code.value}: {message}")


class ArtifactAlreadyExistsError(ArtifactError):
    default_code = ArtifactErrorCode.ARTIFACT_ALREADY_EXISTS


class ArtifactNotFoundError(ArtifactError):
    default_code = ArtifactErrorCode.ARTIFACT_NOT_FOUND


class ArtifactInvalidError(ArtifactError):
    default_code = ArtifactErrorCode.ARTIFACT_INVALID


class ArtifactCorruptError(ArtifactError):
    default_code = ArtifactErrorCode.ARTIFACT_CORRUPT


class PersistenceIOError(ArtifactError):
    default_code = ArtifactErrorCode.PERSISTENCE_IO_ERROR


class InvalidArtifactPathError(ArtifactError):
    default_code = ArtifactErrorCode.INVALID_ARTIFACT_PATH


class ArtifactSchemaValidationError(ArtifactError):
    default_code = ArtifactErrorCode.SCHEMA_VALIDATION_FAILED


class ShadowRunErrorCode(str, Enum):
    """Stable stage failures for read-only shadow composition."""

    MARKET_DATA_FAILED = "MARKET_DATA_FAILED"
    SENTIMENT_FAILED = "SENTIMENT_FAILED"
    LEDGER_FAILED = "LEDGER_FAILED"
    DECISION_FAILED = "DECISION_FAILED"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    RUN_ALREADY_COMPLETED = "RUN_ALREADY_COMPLETED"


class ShadowRunError(BtcDcaError):
    """A shadow stage failed without producing a completed run manifest."""

    def __init__(
        self,
        message: str,
        *,
        code: ShadowRunErrorCode,
        cause: Exception | None = None,
    ) -> None:
        self.code = code
        self.stage = code.value.removesuffix("_FAILED")
        self.cause = cause
        super().__init__(f"{code.value}: {message}")


class ShadowRunAlreadyCompletedError(ShadowRunError):
    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"completed shadow run already exists: {run_id}",
            code=ShadowRunErrorCode.RUN_ALREADY_COMPLETED,
        )


class NotificationErrorCode(str, Enum):
    """Stable delivery failures for notification callers."""

    NOTIFICATION_TIMEOUT = "NOTIFICATION_TIMEOUT"
    NOTIFICATION_RATE_LIMITED = "NOTIFICATION_RATE_LIMITED"
    NOTIFICATION_HTTP_ERROR = "NOTIFICATION_HTTP_ERROR"
    NOTIFICATION_INVALID_RESPONSE = "NOTIFICATION_INVALID_RESPONSE"
    NOTIFICATION_FAILED = "NOTIFICATION_FAILED"


class NotificationError(BtcDcaError):
    """Telegram delivery failed without changing canonical run state."""

    def __init__(
        self,
        message: str,
        *,
        code: NotificationErrorCode,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(f"{code.value}: {message}")
