"""Production infrastructure composition for the sole V2 writer path."""
from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..core.models import AccountIdentity, OrderIntent
from ..models import MarketSnapshot
from ..engine import calculate_decision
from ..config import load_strategy_config
from ..artifacts import ArtifactStore, ArtifactType
from ..execution_v2.composition import V2ExecutionRuntime, build_v2_execution_runtime
from ..infrastructure.accounting_repository import JsonAccountingRepository
from ..infrastructure.execution_repository import JsonExecutionRepository
from ..infrastructure.policy import load_runtime_execution_policy
from ..ledger import read_executions
from ..private_bybit import BybitPrivateReadClient, credential_scope_allows_spot_execution
from ..risk.engine import MarketEvidenceBinding, RiskContext
from ..sentiment.alternative_me import SOURCE as SENTIMENT_SOURCE
from ..market_data.bybit import BYBIT_SOURCE
from ..schemas import validate_artifact
from ..venues.ccxt.adapter import CcxtVenueAdapter
from ..venues.health import VenueCircuitBreaker

MAX_DECISION_AGE = timedelta(minutes=15)
MAX_CLOCK_SKEW = timedelta(minutes=5)
EXPECTED_BYBIT_ENVIRONMENT = "mainnet"


def _account_identity_fingerprint(*, identity: str | None, environment: str | None, account: object) -> str:
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("provider-backed account identity is unavailable")
    if environment != EXPECTED_BYBIT_ENVIRONMENT:
        raise ValueError("authenticated Bybit environment is not production mainnet")
    material = "|".join(
        (
            "Bybit",
            environment,
            identity,
            str(account.unified_margin_status),
            str(account.margin_mode),
            str(account.spot_hedging_status),
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


class BybitProductionAccountContext:
    """Fresh read-only account context used by Risk immediately pre-submit."""

    def __init__(
        self,
        client: BybitPrivateReadClient,
        *,
        oms: JsonExecutionRepository,
        breaker: VenueCircuitBreaker,
        artifact_store: ArtifactStore | None = None,
        now=None,
    ) -> None:
        self.client = client
        self.oms = oms
        self.breaker = breaker
        self.artifact_store = artifact_store
        self.now = now or (lambda: datetime.now(UTC))

    def _market_evidence(self, intent: OrderIntent, now: datetime) -> MarketEvidenceBinding | None:
        if self.artifact_store is None or not all(
            (
                intent.market_snapshot_id,
                intent.market_snapshot_sha256,
                intent.sentiment_snapshot_id,
                intent.sentiment_snapshot_sha256,
                intent.fear_greed_index is not None,
            )
        ):
            return None
        try:
            decision, _ = self.artifact_store.find_artifact(
                ArtifactType.DECISION,
                identity_field="decision_id",
                identity_value=intent.decision_id,
            )
            market, market_sha = self.artifact_store.find_artifact(
                ArtifactType.MARKET,
                identity_field="snapshot_id",
                identity_value=str(intent.market_snapshot_id),
            )
            sentiment, sentiment_sha = self.artifact_store.find_artifact(
                ArtifactType.SENTIMENT,
                identity_field="snapshot_id",
                identity_value=str(intent.sentiment_snapshot_id),
            )
            validate_artifact("decision", decision)
            validate_artifact("market_snapshot", market)
            validate_artifact("sentiment_snapshot", sentiment)
            if any(
                (
                    decision.get("decision_id") != intent.decision_id,
                    decision.get("market_snapshot_id") != intent.market_snapshot_id,
                    decision.get("market_snapshot_sha256") != intent.market_snapshot_sha256,
                    decision.get("sentiment_snapshot_id") != intent.sentiment_snapshot_id,
                    decision.get("sentiment_snapshot_sha256") != intent.sentiment_snapshot_sha256,
                    decision.get("fear_greed_index") != intent.fear_greed_index,
                    market_sha != intent.market_snapshot_sha256,
                    sentiment_sha != intent.sentiment_snapshot_sha256,
                )
            ):
                return None
            market_validation = market.get("validation", {})
            market_metadata = market.get("market_data_metadata", {})
            if not all(
                (
                    market.get("source_exchange") == "Bybit",
                    market.get("market_type") == "spot",
                    market.get("symbol") == "BTCUSDT",
                    market_validation.get("same_source_price_and_high") is True,
                    market_validation.get("full_168h_coverage") is True,
                    market_validation.get("fresh") is True,
                    market_metadata.get("source") == BYBIT_SOURCE,
                    market_metadata.get("external_symbol") == "BYBIT:BTCUSDT",
                )
            ):
                return None
            captured = datetime.fromisoformat(str(market["captured_at_utc"]).replace("Z", "+00:00")).astimezone(UTC)
            if captured > now or now - captured > MAX_DECISION_AGE:
                return None
            sentiment_validation = sentiment.get("validation", {})
            if not all(
                (
                    sentiment.get("source") == SENTIMENT_SOURCE,
                    sentiment.get("index_name") == "Crypto Fear & Greed Index",
                    sentiment.get("value") == intent.fear_greed_index,
                    sentiment_validation.get("value_in_range") is True,
                    sentiment_validation.get("fresh") is True,
                    sentiment_validation.get("source_identity_valid") is True,
                )
            ):
                return None
            retrieved = datetime.fromisoformat(str(sentiment["retrieved_at_utc"]).replace("Z", "+00:00")).astimezone(UTC)
            observed = datetime.fromisoformat(str(sentiment["observed_at_utc"]).replace("Z", "+00:00")).astimezone(UTC)
            if retrieved > now or observed > retrieved or now - observed > timedelta(hours=36):
                return None
            metadata = market["market_data_metadata"]
            canonical_decision = calculate_decision(
                MarketSnapshot(
                    schema_version=str(market["schema_version"]),
                    snapshot_id=str(market["snapshot_id"]),
                    captured_at_utc=str(market["captured_at_utc"]),
                    source_exchange=str(market["source_exchange"]),
                    market_type=str(market["market_type"]),
                    symbol=str(market["symbol"]),
                    current_price_usdt=Decimal(str(market["current_price_usdt"])),
                    rolling_7d_high_usdt=Decimal(str(market["rolling_7d_high_usdt"])),
                    window_start_utc=str(market["window_start_utc"]),
                    window_end_utc=str(market["window_end_utc"]),
                    same_source_price_and_high=market["validation"]["same_source_price_and_high"],
                    full_168h_coverage=market["validation"]["full_168h_coverage"],
                    fresh=market["validation"]["fresh"],
                    observation_resolution_seconds=int(metadata["observation_resolution_seconds"]),
                    trade_level_exact=metadata["trade_level_exact"],
                    source=str(metadata["source"]),
                    external_symbol=str(metadata["external_symbol"]),
                    primary_source=str(metadata.get("primary_source", "bybit_api")),
                    primary_failure_category=metadata.get("primary_failure_category"),
                    fallback_attempted=metadata.get("fallback_attempted", False),
                ),
                int(sentiment["value"]),
                Decimal(str(decision["monthly_spent_before_usd"])),
                load_strategy_config(),
            ).to_dict()
            economic_fields = (
                "drawdown_percent",
                "fear_greed_index",
                "base_allocation_usd",
                "sentiment_multiplier",
                "calculated_allocation_usd",
                "monthly_spent_before_usd",
                "remaining_budget_before_usd",
                "final_purchase_usd",
                "status",
            )
            if any(decision.get(field) != canonical_decision.get(field) for field in economic_fields):
                return None
        except Exception:
            return None
        return MarketEvidenceBinding(
            decision_id=intent.decision_id,
            market_snapshot_id=str(intent.market_snapshot_id),
            market_snapshot_sha256=str(intent.market_snapshot_sha256),
            sentiment_snapshot_id=str(intent.sentiment_snapshot_id),
            sentiment_snapshot_sha256=str(intent.sentiment_snapshot_sha256),
            verified=True,
            fear_greed_index=int(intent.fear_greed_index),
            final_purchase_usd=Decimal(str(decision["final_purchase_usd"])),
        )

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
            fingerprint = _account_identity_fingerprint(
                identity=credential.identity,
                environment=self.client.environment,
                account=account,
            )
        except Exception as exc:
            self.breaker.record_failure(type(exc).__name__)
            raise

        self.breaker.record_success()
        created = datetime.fromisoformat(intent.created_at_utc.replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("intent timestamp must be timezone-aware")
        age = now - created.astimezone(UTC)
        clock_skew = abs(
            datetime.fromtimestamp(server_time_ms / 1000, tz=UTC) - now
        )
        fresh = timedelta(0) <= age <= MAX_DECISION_AGE and clock_skew <= MAX_CLOCK_SKEW
        market_evidence = self._market_evidence(intent, now)
        fresh = fresh and market_evidence is not None
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
            market_evidence=market_evidence,
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
        artifact_store=ArtifactStore(data_root),
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
