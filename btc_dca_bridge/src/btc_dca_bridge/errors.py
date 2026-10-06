"""Domain-specific failures for the offline BTC DCA core."""


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
