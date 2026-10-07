"""Read-only composition of one deterministic BTC DCA shadow run."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from .artifacts import ArtifactReceipt, ArtifactStore, ArtifactType, make_run_id
from .config import (
    MarketDataConfig, OperationalConfig, SentimentConfig, StrategyConfig,
    load_market_data_config, load_operational_config, load_sentiment_config,
    load_strategy_config,
)
from .engine import calculate_decision
from .errors import (
    ArtifactError,
    ArtifactNotFoundError,
    ShadowRunAlreadyCompletedError,
    ShadowRunError,
    ShadowRunErrorCode,
)
from .ledger import read_executions
from .market_data.bybit import BybitSpotAdapter
from .market_data.http import PublicHttpTransport
from .market_data.provider import BybitSnapshotSource, FallbackMarketDataProvider, TradingViewSnapshotSource
from .market_data.tradingview import TradingViewBybitSpotAdapter, WebSocketTradingViewTransport
from .models import Execution, MarketSnapshot, PortfolioState, StrategyDecision
from .paths import CONFIG_PATH, DATA_PATH, LEDGER_PATH, PROJECT_ROOT
from .portfolio import derive_portfolio
from .schemas import validate_artifact
from .sentiment import AlternativeMeFearGreedAdapter, SentimentSnapshot


class MarketProvider(Protocol):
    def get_market_snapshot(self, *, captured_at_utc: datetime | None = None) -> MarketSnapshot: ...


class SentimentProvider(Protocol):
    def fetch_current(self) -> SentimentSnapshot: ...


@dataclass(frozen=True)
class ShadowRunResult:
    run_id: str
    started_at_utc: str
    completed_at_utc: str
    market_snapshot: MarketSnapshot
    sentiment_snapshot: SentimentSnapshot
    portfolio_state: PortfolioState
    decision: StrategyDecision
    market_receipt: ArtifactReceipt
    sentiment_receipt: ArtifactReceipt
    decision_receipt: ArtifactReceipt
    manifest: dict[str, Any]
    manifest_receipt: ArtifactReceipt
    mode: str = "shadow"
    status: str = "completed"
    no_order_executed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at_utc": self.started_at_utc,
            "completed_at_utc": self.completed_at_utc,
            "mode": self.mode,
            "status": self.status,
            "market_snapshot": self.market_snapshot.to_dict(),
            "sentiment_snapshot": self.sentiment_snapshot.to_dict(),
            "portfolio_state": self.portfolio_state.to_dict(),
            "decision": self.decision.to_dict(),
            "manifest": dict(self.manifest),
            "manifest_path": str(self.manifest_receipt.path),
            "no_order_executed": self.no_order_executed,
        }


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("shadow run time must be a UTC-aware datetime")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _default_run_id(run_at_utc: datetime) -> str:
    suffix = hashlib.sha256(_iso(run_at_utc).encode("utf-8")).hexdigest()[:12]
    return make_run_id(run_at_utc, suffix)


def _number(value: Any) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


class ShadowPipeline:
    """Compose canonical components without executing or mutating a purchase."""

    def __init__(
        self,
        *,
        market_provider: MarketProvider,
        sentiment_provider: SentimentProvider,
        strategy: StrategyConfig,
        artifact_store: ArtifactStore,
        ledger_path: Path = LEDGER_PATH,
        ledger_reader: Callable[[Path], tuple[Execution, ...]] = read_executions,
        decision_calculator: Callable[[MarketSnapshot, int, Any, StrategyConfig], StrategyDecision] = calculate_decision,
    ) -> None:
        self.market_provider = market_provider
        self.sentiment_provider = sentiment_provider
        self.strategy = strategy
        self.artifact_store = artifact_store
        self.ledger_path = Path(ledger_path)
        self._ledger_reader = ledger_reader
        self._decision_calculator = decision_calculator

    def run(
        self,
        *,
        run_at_utc: datetime,
        run_id: str | None = None,
        run_identity_at_utc: datetime | None = None,
    ) -> ShadowRunResult:
        run_at = _utc(run_at_utc)
        identity_at = _utc(run_identity_at_utc) if run_identity_at_utc is not None else run_at
        shared_run_id = run_id or _default_run_id(identity_at)
        # make_run_id performs the canonical path-safety validation even when supplied.
        if run_id is not None:
            try:
                suffix = shared_run_id.split("_", 2)[-1] if isinstance(shared_run_id, str) else ""
                expected_prefix = f"run_{identity_at.strftime('%Y%m%dT%H%M%SZ')}_"
                if not isinstance(shared_run_id, str) or not shared_run_id.startswith(expected_prefix):
                    raise ValueError("run identity timestamp mismatch")
                make_run_id(identity_at, suffix)
            except Exception as exc:
                self._raise(ShadowRunErrorCode.PERSISTENCE_FAILED, "invalid run identity", exc)
        self._reject_completed(shared_run_id)

        try:
            market = self.market_provider.get_market_snapshot(captured_at_utc=run_at)
            validate_artifact("market_snapshot", market.to_dict())
            if market.captured_at_utc != _iso(run_at):
                raise ValueError("market snapshot capture time does not match run context")
        except Exception as exc:
            self._raise(ShadowRunErrorCode.MARKET_DATA_FAILED, "market snapshot acquisition failed", exc)

        try:
            sentiment = self.sentiment_provider.fetch_current()
            validate_artifact("sentiment_snapshot", sentiment.to_dict())
            if sentiment.retrieved_at_utc != _iso(run_at):
                raise ValueError("sentiment retrieval time does not match run context")
        except Exception as exc:
            self._raise(ShadowRunErrorCode.SENTIMENT_FAILED, "sentiment snapshot acquisition failed", exc)

        calendar_month = run_at.strftime("%Y-%m")
        try:
            executions = self._ledger_reader(self.ledger_path)
            portfolio = derive_portfolio(executions, calendar_month, self.strategy.monthly_cap_usd)
            validate_artifact("portfolio_state", portfolio.to_dict())
        except Exception as exc:
            self._raise(ShadowRunErrorCode.LEDGER_FAILED, "canonical ledger derivation failed", exc)

        try:
            decision = self._decision_calculator(
                market,
                sentiment.value,
                portfolio.monthly_confirmed_usd_deployed,
                self.strategy,
            )
            validate_artifact("decision", decision.to_dict())
        except Exception as exc:
            self._raise(ShadowRunErrorCode.DECISION_FAILED, "V1 decision calculation failed", exc)

        try:
            market_receipt = self.artifact_store.persist(ArtifactType.MARKET, market, run_id=shared_run_id)
            sentiment_receipt = self.artifact_store.persist(ArtifactType.SENTIMENT, sentiment, run_id=shared_run_id)
            decision_receipt = self.artifact_store.persist(ArtifactType.DECISION, decision, run_id=shared_run_id)
            manifest = self._manifest(
                shared_run_id,
                run_at,
                market,
                sentiment,
                portfolio,
                decision,
                market_receipt,
                sentiment_receipt,
                decision_receipt,
            )
            manifest_receipt = self.artifact_store.persist(ArtifactType.RUN, manifest, run_id=shared_run_id)
        except Exception as exc:
            self._raise(ShadowRunErrorCode.PERSISTENCE_FAILED, "immutable artifact publication failed", exc)

        instant = _iso(run_at)
        return ShadowRunResult(
            shared_run_id,
            instant,
            instant,
            market,
            sentiment,
            portfolio,
            decision,
            market_receipt,
            sentiment_receipt,
            decision_receipt,
            manifest,
            manifest_receipt,
        )

    def _reject_completed(self, run_id: str) -> None:
        try:
            self.artifact_store.find_completed_run(run_id=run_id)
        except ArtifactNotFoundError:
            return
        except ArtifactError as exc:
            self._raise(ShadowRunErrorCode.PERSISTENCE_FAILED, "cannot verify prior run state", exc)
        raise ShadowRunAlreadyCompletedError(run_id)

    def _receipt(self, receipt: ArtifactReceipt) -> dict[str, Any]:
        try:
            relative = receipt.path.relative_to(self.artifact_store.root).as_posix()
        except ValueError as exc:
            raise ArtifactError("artifact receipt escaped configured root") from exc
        return {
            "artifact_type": receipt.artifact_type.value,
            "path": relative,
            "schema_version": receipt.schema_version,
            "sha256": receipt.sha256,
            "byte_length": receipt.byte_length,
        }

    def _manifest(
        self,
        run_id: str,
        run_at: datetime,
        market: MarketSnapshot,
        sentiment: SentimentSnapshot,
        portfolio: PortfolioState,
        decision: StrategyDecision,
        market_receipt: ArtifactReceipt,
        sentiment_receipt: ArtifactReceipt,
        decision_receipt: ArtifactReceipt,
    ) -> dict[str, Any]:
        decision_payload = decision.to_dict()
        manifest = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "started_at_utc": _iso(run_at),
            "completed_at_utc": _iso(run_at),
            "mode": "shadow",
            "status": "completed",
            "market_artifact": self._receipt(market_receipt),
            "sentiment_artifact": self._receipt(sentiment_receipt),
            "decision_artifact": self._receipt(decision_receipt),
            "market_snapshot_id": market.snapshot_id,
            "sentiment_snapshot_id": sentiment.snapshot_id,
            "decision_id": decision_payload["decision_id"],
            "selected_market_source": market.source,
            "primary_market_source": market.primary_source,
            "primary_failure_category": market.primary_failure_category,
            "fallback_attempted": market.fallback_attempted,
            "strategy_id": self.strategy.strategy_id,
            "strategy_version": self.strategy.strategy_version,
            "calendar_month": portfolio.calendar_month,
            "monthly_spend_source": "canonical_execution_ledger",
            "monthly_spent_usd": _number(portfolio.monthly_confirmed_usd_deployed),
            "remaining_monthly_budget_usd": _number(portfolio.remaining_monthly_budget_usd),
            "final_purchase_usd": decision_payload["final_purchase_usd"],
            "no_order_executed": True,
        }
        validate_artifact("shadow_run", manifest)
        return manifest

    @staticmethod
    def _raise(code: ShadowRunErrorCode, message: str, cause: Exception) -> None:
        raise ShadowRunError(message, code=code, cause=cause) from cause


def build_live_shadow_pipeline(
    *,
    run_at_utc: datetime,
    config_path: Path = CONFIG_PATH,
    ledger_path: Path = LEDGER_PATH,
    data_root: Path | None = None,
    operational_config: OperationalConfig | None = None,
) -> ShadowPipeline:
    """Build the public-read-only pipeline; no credentials or order client exist."""
    run_at = _utc(run_at_utc)
    clock = lambda: run_at
    operational = operational_config or load_operational_config()
    market_config = operational.market_data
    sentiment_config = operational.sentiment
    market_transport = PublicHttpTransport(
        "https://api.bybit.com",
        connect_timeout_seconds=market_config.http.connect_timeout_seconds,
        read_timeout_seconds=market_config.http.read_timeout_seconds,
        max_attempts=market_config.http.retry_attempts,
        backoff_seconds=market_config.http.backoff_seconds,
    )
    primary = BybitSnapshotSource(
        BybitSpotAdapter(
            transport=market_transport, clock=clock,
            page_limit=market_config.candle_page_limit,
            max_pages=market_config.candle_max_pages,
        ),
        clock=clock,
        max_input_age=timedelta(seconds=market_config.freshness_max_age_seconds),
    )
    fallback = TradingViewSnapshotSource(
        TradingViewBybitSpotAdapter(
            transport=WebSocketTradingViewTransport(
                timeout_seconds=market_config.tradingview_timeout_seconds
            ),
            clock=clock,
        ),
        clock=clock,
        max_input_age=timedelta(seconds=market_config.freshness_max_age_seconds),
        max_observation_age=timedelta(
            seconds=market_config.tradingview_observation_max_age_seconds
        ),
    )
    market_provider = FallbackMarketDataProvider(primary, fallback, clock=clock)
    sentiment_transport = PublicHttpTransport(
        "https://api.alternative.me",
        connect_timeout_seconds=sentiment_config.http.connect_timeout_seconds,
        read_timeout_seconds=sentiment_config.http.read_timeout_seconds,
        max_attempts=sentiment_config.http.retry_attempts,
        backoff_seconds=sentiment_config.http.backoff_seconds,
    )
    sentiment_provider = AlternativeMeFearGreedAdapter(
        transport=sentiment_transport,
        clock=clock,
        max_observation_age=timedelta(seconds=sentiment_config.freshness_max_age_seconds),
    )
    return ShadowPipeline(
        market_provider=market_provider,
        sentiment_provider=sentiment_provider,
        strategy=load_strategy_config(config_path),
        artifact_store=ArtifactStore(
            Path(data_root) if data_root is not None else PROJECT_ROOT / operational.persistence.artifact_root
        ),
        ledger_path=ledger_path,
    )


def format_shadow_output(result: ShadowRunResult) -> str:
    decision = result.decision.to_dict()
    snapshot = result.market_snapshot
    lines = [
        "mode: SHADOW",
        f"run_id: {result.run_id}",
        f"market source: {snapshot.source}",
        f"BTC price: ${snapshot.current_price_usdt}",
        f"rolling 7D high: ${snapshot.rolling_7d_high_usdt}",
        f"drawdown: {decision['drawdown_percent']}%",
        f"Fear & Greed: {result.sentiment_snapshot.value}",
        f"base allocation: ${decision['base_allocation_usd']}",
        f"sentiment multiplier: x{decision['sentiment_multiplier']}",
        f"calculated allocation: ${decision['calculated_allocation_usd']}",
        f"monthly spent: ${decision['monthly_spent_before_usd']}",
        f"remaining budget: ${decision['remaining_budget_before_usd']}",
        f"FINAL PURCHASE: ${decision['final_purchase_usd']}",
        f"manifest: {result.manifest_receipt.path}",
        f"SHADOW — BUY ${decision['final_purchase_usd']} BTC TODAY — NO ORDER EXECUTED",
    ]
    return "\n".join(lines)
