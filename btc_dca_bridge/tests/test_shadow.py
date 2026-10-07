import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType, make_run_id
from btc_dca_bridge.cli import main
from btc_dca_bridge.config import load_strategy_config
from btc_dca_bridge.errors import (
    ArtifactNotFoundError,
    DataUnavailableError,
    LedgerValidationError,
    PersistenceIOError,
    SentimentSourceUnavailableError,
    ShadowRunAlreadyCompletedError,
    ShadowRunError,
    ShadowRunErrorCode,
)
from btc_dca_bridge.market_data.bybit import BYBIT_SOURCE
from btc_dca_bridge.market_data.provider import FallbackMarketDataProvider
from btc_dca_bridge.market_data.tradingview import TRADINGVIEW_SOURCE
from btc_dca_bridge.models import MarketSnapshot, StrategyDecision
from btc_dca_bridge.sentiment.models import SentimentSnapshot
from btc_dca_bridge.shadow import ShadowPipeline


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def market_snapshot(source=BYBIT_SOURCE):
    return MarketSnapshot(
        schema_version="1.0.0", snapshot_id=f"market_{source}",
        captured_at_utc="2026-10-07T12:00:00Z", source_exchange="Bybit",
        market_type="spot", symbol="BTCUSDT", current_price_usdt=Decimal("80000"),
        rolling_7d_high_usdt=Decimal("100000"), window_start_utc="2026-09-30T12:00:00Z",
        window_end_utc="2026-10-07T12:00:00Z", same_source_price_and_high=True,
        full_168h_coverage=True, fresh=True, source=source, external_symbol="BYBIT:BTCUSDT",
        primary_source=BYBIT_SOURCE,
    )


def sentiment_snapshot():
    return SentimentSnapshot(
        schema_version="1.0.0", snapshot_id="sentiment_test", source="alternative_me_crypto_fear_greed",
        index_name="Crypto Fear & Greed Index", value=35, classification="Fear",
        observed_at_utc="2026-10-07T00:00:00Z", retrieved_at_utc="2026-10-07T12:00:00Z",
        upstream_identifier="https://api.alternative.me/fng/?limit=1&format=json",
    )


def execution(execution_id, executed_at, amount):
    return {
        "schema_version": "1.0.0", "execution_id": execution_id,
        "executed_at_utc": executed_at, "asset": "BTC", "quote_currency": "USDT",
        "executed_usd": amount, "reference_price_usdt": 80000, "btc_quantity": None,
        "status": "reconciled",
        "reconciliation": {"source": "Chat 03 — Portfolio & Budget Tracker", "note": "test fixture"},
    }


class StaticSource:
    def __init__(self, source, value):
        self.source = source
        self.value = value
        self.calls = []

    def get_market_snapshot(self, *, captured_at_utc=None):
        self.calls.append(captured_at_utc)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class StaticSentiment:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def fetch_current(self):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FailOnSentimentStore(ArtifactStore):
    def persist(self, artifact_type, artifact, *, run_id):
        if ArtifactType(artifact_type) is ArtifactType.SENTIMENT:
            raise PersistenceIOError("simulated persistence failure")
        return super().persist(artifact_type, artifact, run_id=run_id)


class ShadowPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data"
        self.ledger_path = self.root / "executions.jsonl"
        self.strategy = load_strategy_config()
        self.write_ledger(execution("execution_oct", "2026-10-05T00:00:00Z", 60))

    def tearDown(self):
        self.temporary.cleanup()

    def write_ledger(self, *records):
        self.ledger_path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    def pipeline(self, *, market=None, sentiment=None, store=None, decision_calculator=None, ledger_reader=None):
        primary = StaticSource(BYBIT_SOURCE, market or market_snapshot())
        fallback = StaticSource(TRADINGVIEW_SOURCE, market_snapshot(TRADINGVIEW_SOURCE))
        provider = FallbackMarketDataProvider(primary, fallback, clock=lambda: NOW)
        kwargs = {}
        if decision_calculator is not None:
            kwargs["decision_calculator"] = decision_calculator
        if ledger_reader is not None:
            kwargs["ledger_reader"] = ledger_reader
        return ShadowPipeline(
            market_provider=provider,
            sentiment_provider=StaticSentiment(sentiment or sentiment_snapshot()),
            strategy=self.strategy,
            artifact_store=store or ArtifactStore(self.data_root),
            ledger_path=self.ledger_path,
            **kwargs,
        )

    def test_successful_direct_bybit_run_uses_ledger_and_exact_v1_result(self):
        result = self.pipeline().run(run_at_utc=NOW)
        decision = result.decision.to_dict()
        self.assertEqual(result.market_snapshot.source, BYBIT_SOURCE)
        self.assertEqual(result.portfolio_state.monthly_confirmed_usd_deployed, Decimal("60"))
        self.assertEqual(decision["drawdown_percent"], -20)
        self.assertEqual(decision["base_allocation_usd"], 75)
        self.assertEqual(decision["sentiment_multiplier"], 1.3)
        self.assertEqual(decision["calculated_allocation_usd"], 98)
        self.assertEqual(decision["monthly_spent_before_usd"], 60)
        self.assertEqual(decision["remaining_budget_before_usd"], 440)
        self.assertEqual(decision["final_purchase_usd"], 98)
        self.assertTrue(result.no_order_executed)

    def test_successful_tradingview_fallback_retains_primary_failure(self):
        primary = StaticSource(BYBIT_SOURCE, DataUnavailableError("offline"))
        fallback = StaticSource(TRADINGVIEW_SOURCE, market_snapshot(TRADINGVIEW_SOURCE))
        pipeline = ShadowPipeline(
            market_provider=FallbackMarketDataProvider(primary, fallback, clock=lambda: NOW),
            sentiment_provider=StaticSentiment(sentiment_snapshot()), strategy=self.strategy,
            artifact_store=ArtifactStore(self.data_root), ledger_path=self.ledger_path,
        )
        result = pipeline.run(run_at_utc=NOW)
        self.assertEqual(result.market_snapshot.source, TRADINGVIEW_SOURCE)
        self.assertTrue(result.market_snapshot.fallback_attempted)
        self.assertEqual(result.market_snapshot.primary_failure_category, "SOURCE_UNAVAILABLE")
        self.assertEqual(result.manifest["selected_market_source"], TRADINGVIEW_SOURCE)

    def test_one_run_id_correlates_every_artifact_and_readback(self):
        result = self.pipeline().run(run_at_utc=NOW)
        for receipt in (result.market_receipt, result.sentiment_receipt, result.decision_receipt, result.manifest_receipt):
            self.assertEqual(receipt.run_id, result.run_id)
            self.assertEqual(receipt.path.stem.split(".json")[0], result.run_id)
        store = ArtifactStore(self.data_root)
        self.assertEqual(store.read("market", run_id=result.run_id, artifact_date_utc=NOW)["snapshot_id"], result.market_snapshot.snapshot_id)
        self.assertEqual(store.read("sentiment", run_id=result.run_id, artifact_date_utc=NOW)["snapshot_id"], result.sentiment_snapshot.snapshot_id)
        self.assertEqual(store.read("decision", run_id=result.run_id, artifact_date_utc=NOW)["decision_id"], result.decision.to_dict()["decision_id"])
        self.assertEqual(store.read("run", run_id=result.run_id, artifact_date_utc=NOW), result.manifest)

    def test_hard_cap_and_below_minimum_remaining_produce_zero(self):
        for amount in (500, 491):
            with self.subTest(amount=amount):
                root = self.root / str(amount)
                ledger = root / "ledger.jsonl"
                root.mkdir()
                ledger.write_text(json.dumps(execution(f"execution_{amount}", "2026-10-01T00:00:00Z", amount)) + "\n")
                pipeline = ShadowPipeline(
                    market_provider=StaticSource(BYBIT_SOURCE, market_snapshot()),
                    sentiment_provider=StaticSentiment(sentiment_snapshot()), strategy=self.strategy,
                    artifact_store=ArtifactStore(root / "data"), ledger_path=ledger,
                )
                result = pipeline.run(run_at_utc=NOW)
                self.assertEqual(result.decision.to_dict()["final_purchase_usd"], 0)

    def test_utc_month_boundary_selects_calendar_month_only(self):
        self.write_ledger(
            execution("execution_sep", "2026-09-30T23:59:59Z", 491),
            execution("execution_oct", "2026-10-01T00:00:00Z", 60),
        )
        sep_time = datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
        sep_market = replace(market_snapshot(), captured_at_utc="2026-09-30T23:59:59Z", window_start_utc="2026-09-23T23:59:59Z", window_end_utc="2026-09-30T23:59:59Z")
        sep_sentiment = replace(sentiment_snapshot(), observed_at_utc="2026-09-30T00:00:00Z", retrieved_at_utc="2026-09-30T23:59:59Z")
        sep = self.pipeline(market=sep_market, sentiment=sep_sentiment, store=ArtifactStore(self.root / "sep")).run(run_at_utc=sep_time)
        oct_time = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
        oct_market = replace(market_snapshot(), captured_at_utc="2026-10-01T00:00:00Z", window_start_utc="2026-09-24T00:00:00Z", window_end_utc="2026-10-01T00:00:00Z")
        oct_sentiment = replace(sentiment_snapshot(), observed_at_utc="2026-09-30T00:00:00Z", retrieved_at_utc="2026-10-01T00:00:00Z")
        oct_result = self.pipeline(market=oct_market, sentiment=oct_sentiment, store=ArtifactStore(self.root / "oct")).run(run_at_utc=oct_time)
        self.assertEqual(sep.portfolio_state.calendar_month, "2026-09")
        self.assertEqual(sep.portfolio_state.monthly_confirmed_usd_deployed, Decimal("491"))
        self.assertEqual(oct_result.portfolio_state.calendar_month, "2026-10")
        self.assertEqual(oct_result.portfolio_state.monthly_confirmed_usd_deployed, Decimal("60"))

    def test_acquisition_ledger_and_decision_failures_create_no_artifacts(self):
        market_failure = self.pipeline()
        market_failure.market_provider = StaticSource(BYBIT_SOURCE, DataUnavailableError("offline"))
        cases = (
            ("market", market_failure, ShadowRunErrorCode.MARKET_DATA_FAILED),
            ("sentiment", self.pipeline(sentiment=SentimentSourceUnavailableError("offline")), ShadowRunErrorCode.SENTIMENT_FAILED),
            ("ledger", self.pipeline(ledger_reader=lambda path: (_ for _ in ()).throw(LedgerValidationError("bad"))), ShadowRunErrorCode.LEDGER_FAILED),
            ("decision", self.pipeline(decision_calculator=lambda *args: StrategyDecision({"schema_version": "bad"})), ShadowRunErrorCode.DECISION_FAILED),
        )
        for label, pipeline, expected in cases:
            with self.subTest(label=label):
                with self.assertRaises(ShadowRunError) as raised:
                    pipeline.run(run_at_utc=NOW, run_id=make_run_id(NOW, f"{label}12345678"))
                self.assertEqual(raised.exception.code, expected)
        self.assertFalse(list(self.data_root.rglob("*.json")) if self.data_root.exists() else [])

    def test_persistence_failure_leaves_partial_evidence_without_completion_manifest(self):
        store = FailOnSentimentStore(self.data_root)
        with self.assertRaises(ShadowRunError) as raised:
            self.pipeline(store=store).run(run_at_utc=NOW)
        self.assertEqual(raised.exception.code, ShadowRunErrorCode.PERSISTENCE_FAILED)
        # The generated suffix is deterministic from the exact run timestamp.
        market_files = list((self.data_root / "market").rglob("*.json"))
        self.assertEqual(len(market_files), 1)
        actual_run_id = market_files[0].stem
        with self.assertRaises(ArtifactNotFoundError):
            store.read("run", run_id=actual_run_id, artifact_date_utc=NOW)
        self.assertFalse(list((self.data_root / "runs").rglob("*.json")) if (self.data_root / "runs").exists() else [])

    def test_completed_duplicate_and_conflicting_partial_run_fail_without_overwrite(self):
        pipeline = self.pipeline()
        first = pipeline.run(run_at_utc=NOW)
        with self.assertRaises(ShadowRunAlreadyCompletedError):
            pipeline.run(run_at_utc=NOW, run_id=first.run_id)
        with self.assertRaises(ShadowRunAlreadyCompletedError):
            pipeline.run(
                run_at_utc=datetime(2026, 10, 8, 0, 1, tzinfo=UTC),
                run_id=first.run_id,
                run_identity_at_utc=NOW,
            )

        partial_root = self.root / "partial"
        store = ArtifactStore(partial_root)
        conflict_id = make_run_id(NOW, "deadbeef1234")
        conflict = replace(market_snapshot(), current_price_usdt=Decimal("79000"))
        store.persist("market", conflict, run_id=conflict_id)
        with self.assertRaises(ShadowRunError) as raised:
            self.pipeline(store=store).run(run_at_utc=NOW, run_id=conflict_id)
        self.assertEqual(raised.exception.code, ShadowRunErrorCode.PERSISTENCE_FAILED)
        self.assertEqual(store.read("market", run_id=conflict_id, artifact_date_utc=NOW)["current_price_usdt"], 79000.0)
        with self.assertRaises(ArtifactNotFoundError):
            store.read("run", run_id=conflict_id, artifact_date_utc=NOW)

    def test_same_inputs_are_deterministic_and_ledger_is_never_mutated(self):
        before = self.ledger_path.read_bytes()
        first = self.pipeline(store=ArtifactStore(self.root / "one")).run(run_at_utc=NOW)
        second = self.pipeline(store=ArtifactStore(self.root / "two")).run(run_at_utc=NOW)
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(first.decision.to_dict(), second.decision.to_dict())
        self.assertEqual(first.manifest, second.manifest)
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertFalse(hasattr(self.pipeline(), "place_order"))

    def test_cli_success_and_failure_exit_status_and_shadow_wording(self):
        result = self.pipeline(store=ArtifactStore(self.root / "cli")).run(run_at_utc=NOW)
        stdout = io.StringIO()
        with patch("btc_dca_bridge.cli._run_shadow", return_value=result), patch("sys.stdout", stdout):
            self.assertEqual(main(["run", "--mode", "shadow", "--run-at", "2026-10-07T12:00:00Z"]), 0)
        output = stdout.getvalue()
        self.assertIn("mode: SHADOW", output)
        self.assertIn("NO ORDER EXECUTED", output)
        self.assertIn("SHADOW — BUY $98 BTC TODAY", output)

        stderr = io.StringIO()
        failure = ShadowRunError("offline", code=ShadowRunErrorCode.MARKET_DATA_FAILED)
        with patch("btc_dca_bridge.cli._run_shadow", side_effect=failure), patch("sys.stderr", stderr):
            self.assertEqual(main(["run", "--mode", "shadow"]), 2)
        self.assertIn("MARKET_DATA_FAILED", stderr.getvalue())

    def test_shadow_module_has_no_legacy_or_order_execution_dependency(self):
        source = (Path(__file__).parents[1] / "src/btc_dca_bridge/shadow.py").read_text()
        self.assertNotIn("Binance", source)
        self.assertNotIn("KuCoin", source)
        self.assertNotIn("place_order", source)


if __name__ == "__main__":
    unittest.main()
