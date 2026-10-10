"""Single source of truth for controlled-execution ceremony TTLs."""

from datetime import timedelta

CANARY_MANIFEST_TTL = timedelta(minutes=30)
LIVE_EXECUTION_APPROVAL_TTL = timedelta(minutes=10)
REAL_MONEY_AUTHORIZATION_TTL = timedelta(minutes=10)
EXECUTION_ACTIVATION_TTL = timedelta(minutes=5)

