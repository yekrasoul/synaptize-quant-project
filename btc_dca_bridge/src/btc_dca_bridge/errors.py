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


class SchemaValidationError(BtcDcaError):
    """An artifact does not conform to its canonical JSON Schema."""


class MarketDataErrorCode(str, Enum):
    """Stable machine-readable failure categories for market-data callers."""

    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    INVALID_MARKET_IDENTITY = "INVALID_MARKET_IDENTITY"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    DATA_STALE = "DATA_STALE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    RATE_LIMITED = "RATE_LIMITED"


class MarketDataError(BtcDcaError):
    """Base failure raised by a market-data source or normalization boundary."""

    default_code = MarketDataErrorCode.DATA_UNAVAILABLE

    def __init__(
        self,
        message: str,
        *,
        code: MarketDataErrorCode | None = None,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code or self.default_code
        self.status_code = status_code
        self.retryable = retryable
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
    """Bybit rejected the public request because of request frequency."""

    default_code = MarketDataErrorCode.RATE_LIMITED
