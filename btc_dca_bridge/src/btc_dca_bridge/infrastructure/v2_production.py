"""Production infrastructure composition for the sole V2 writer path."""
from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..core.models import AccountIdentity, OrderIntent
from ..execution_v2.composition import V2ExecutionRuntime, build_v2_execution_runtime
from ..infrastructure.accounting_repository import JsonAccountingRepository
from ..infrastructure.execution_repository import JsonExecutionRepository
from ..infrastructure.policy import load_runtime_execution_policy
from ..ledger import read_executions
from ..private_bybit import BybitPrivateReadClient, credential_scope_allows_spot_execution
from ..risk.engine import RiskContext
from ..venues.ccxt.adapter import CcxtVenueAdapter
from ..venues.health import VenueCircuitBreaker

MAX_DECISION_AGE = timedelta(minutes=15)
MAX_CLOCK_SKEW = timedelta(minutes=5)


class BybitProductionAccountContext:
    """Fresh read-only account context used by Risk immediately pre-submit."""

    def __init__(
        self,
        client: BybitPrivateReadClient,
        *,
        oms: JsonExecutionRepository,
        breaker: VenueCircuitBreaker,
        now=None,
    ) -> None:
        self.client = client
        self.oms = oms
        self.breaker = breaker
        self.now = now or (lambda: datetime.now(UTC))

    def current(self, intent: OrderIntent) -> RiskContext:
        now = self.now().astimezone(UTC)
        try:
            credential = self.client.credential_info()
            if not credential_scope_allows_spot_execution(credential):
                raise ValueError("credential scope is not approved for Spot execution")
            account = self.client.account_info()
            if not all(
                (
                    account.unified_margin_status,
                    account.margin_mode,
                    account.spot_hedging_status,
                    account.updated_time,
                )
            ):
                raise ValueError("account context is incomplete")
            balances = self.client.wallet_balances()
            availability = self.client.spot_quote_availability()
            server_time_ms = self.client.server_time_ms()
        except Exception as exc:
            self.breaker.record_failure(type(exc).__name__)
            raise

        self.breaker.record_success()
        fingerprint = hashlib.sha256(str(account).encode()).hexdigest()
        created = datetime.fromisoformat(intent.created_at_utc.replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("intent timestamp must be timezone-aware")
        age = now - created.astimezone(UTC)
        clock_skew = abs(
            datetime.fromtimestamp(server_time_ms / 1000, tz=UTC) - now
        )
        fresh = timedelta(0) <= age <= MAX_DECISION_AGE and clock_skew <= MAX_CLOCK_SKEW
        unresolved_other = any(
            order.intent.client_order_id != intent.client_order_id
            for order in self.oms.unresolved()
        )
        borrowing = any(balance.has_liability for balance in balances)
        return RiskContext(
            calendar_month=created.astimezone(UTC).strftime("%Y-%m"),
            monthly_spend=Decimal("0"),
            monthly_cap=Decimal("500"),
            runtime_execution_enabled=False,
            kill_switch_active=True,
            market_data_fresh=fresh,
            account_identity=AccountIdentity("bybit", fingerprint),
            expected_account_fingerprint=fingerprint,
            quote_balance=availability.amount_usdt,
            unresolved_execution_exists=unresolved_other,
            duplicate_execution_exists=False,
            venue_health=self.breaker.current().state,
            allowed_venue="bybit",
            allowed_market="BTC/USDT:spot",
            leverage_enabled=False,
            borrowing_enabled=borrowing,
        )


def build_production_v2_runtime(
    *,
    data_root: Path,
    ledger_path: Path,
    policy_path: Path,
) -> V2ExecutionRuntime:
    """Assemble the sole production writer after CLI activation gates pass."""
    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")
    if not api_key or not api_secret:
        raise ValueError("Bybit execution credentials are unavailable")

    oms = JsonExecutionRepository(data_root / "execution_v2")
    accounting = JsonAccountingRepository(data_root / "accounting_v2")
    breaker = VenueCircuitBreaker()
    private_client = BybitPrivateReadClient.from_environment()
    account_context = BybitProductionAccountContext(
        private_client,
        oms=oms,
        breaker=breaker,
    )
    venue = CcxtVenueAdapter.bybit(
        {"api_key": api_key, "api_secret": api_secret},
        identity_factory=lambda payload: hashlib.sha256(repr(payload).encode()).hexdigest(),
    )
    return build_v2_execution_runtime(
        venue=venue,
        oms=oms,
        account_context=account_context,
        policy=lambda: load_runtime_execution_policy(policy_path),
        venue_breaker=breaker,
        legacy_reader=lambda: tuple(read_executions(ledger_path)),
        v2_reader=accounting.read_confirmed,
        accounting_repository=accounting,
    )
