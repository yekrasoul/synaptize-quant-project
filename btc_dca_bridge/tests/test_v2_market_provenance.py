from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType
from btc_dca_bridge.core.models import AccountIdentity, Asset, Market, OrderIntent, OrderType, Side
from btc_dca_bridge.infrastructure.v2_production import BybitProductionAccountContext
from btc_dca_bridge.private_bybit import AccountInfo, ApiCredentialInfo, CredentialClassification, SpotQuoteAvailability
from btc_dca_bridge.execution_v2.pre_submit import plan_pre_submit
from btc_dca_bridge.risk.engine import RiskEngine
from btc_dca_bridge.sentiment.models import SentimentSnapshot
from btc_dca_bridge.models import MarketSnapshot
from btc_dca_bridge.real_money_authorization import artifact_sha256
from btc_dca_bridge.venues.health import VenueCircuitBreaker
from btc_dca_bridge.schemas import validate_artifact
from btc_dca_bridge.errors import SchemaValidationError


NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
ISO_NOW = "2026-10-25T12:00:00Z"


class FakeOms:
    def unresolved(self):
        return ()


class FakeClient:
    environment = "mainnet"

    def credential_info(self):
        return ApiCredentialInfo(
            CredentialClassification.TRADE_CAPABLE,
            False,
            {"Spot": ("SpotTrade",)},
            identity="provider-key-1",
        )

    def account_info(self):
        return AccountInfo(6, "REGULAR_MARGIN", "OFF", "1")

    def wallet_balances(self):
        return ()

    def spot_quote_availability(self):
        return SpotQuoteAvailability(
            Decimal("100"), "/v5/account/wallet-balance", "availableToWithdraw",
            "UNIFIED", True, ISO_NOW,
        )

    def server_time_ms(self):
        return int(NOW.timestamp() * 1000)


class FakeVenue:
    validated = 0

    def validate_intent(self, intent):
        self.validated += 1

    def prepare_submission(self, intent):
        self.validated += 1
        return {}


def market_snapshot(*, captured_at: str = ISO_NOW, fresh: bool = True, source_exchange: str = "Bybit", market_type: str = "spot", same_source: bool = True) -> dict[str, object]:
    return MarketSnapshot(
        "1.0.0", "market_provenance", captured_at, source_exchange, market_type,
        "BTCUSDT", Decimal("90000"), Decimal("100000"),
        "2026-10-18T12:00:00Z", captured_at, same_source, True, fresh,
        source="bybit_api", external_symbol="BYBIT:BTCUSDT",
    ).to_dict()


def sentiment_snapshot(*, observed_at: str = ISO_NOW, retrieved_at: str = ISO_NOW, fresh: bool = True) -> dict[str, object]:
    return SentimentSnapshot(
        "1.0.0", "sentiment_provenance", "alternative_me_crypto_fear_greed",
        "Crypto Fear & Greed Index", 35, "Fear", observed_at, retrieved_at,
        "https://api.alternative.me/fng/?limit=1&format=json", fresh=fresh,
    ).to_dict()


def decision(*, market_sha: str, sentiment_sha: str, include_evidence: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0.0", "decision_id": "decision_provenance",
        "created_at_utc": ISO_NOW, "strategy_id": "btc_adaptive_dca_v1",
        "strategy_version": "1.0.0", "market_snapshot_id": "market_provenance",
        "drawdown_percent": -10, "fear_greed_index": 35,
        "base_allocation_usd": 25, "sentiment_multiplier": 1,
        "calculated_allocation_usd": 25, "monthly_spent_before_usd": 0,
        "remaining_budget_before_usd": 500, "final_purchase_usd": 25,
        "status": "approved",
    }
    if include_evidence:
        payload.update(
            market_snapshot_sha256=market_sha,
            sentiment_snapshot_id="sentiment_provenance",
            sentiment_snapshot_sha256=sentiment_sha,
        )
    return payload


def intent_from(payload: dict[str, object]) -> OrderIntent:
    from btc_dca_bridge.execution_v2.composition import order_intent_from_decision
    return order_intent_from_decision(payload, run_id="run_20261025T120000Z_provenance", created_at_utc=ISO_NOW)


class V2MarketProvenanceTests(unittest.TestCase):
    def build(self, *, market=None, sentiment=None, include_evidence=True):
        root = TemporaryDirectory()
        store = ArtifactStore(Path(root.name))
        market = market or market_snapshot()
        sentiment = sentiment or sentiment_snapshot()
        market_receipt = store.persist(ArtifactType.MARKET, market, run_id="run_20261025T120000Z_provenance")
        sentiment_receipt = store.persist(ArtifactType.SENTIMENT, sentiment, run_id="run_20261025T120000Z_provenance")
        decision_payload = decision(
            market_sha=market_receipt.sha256,
            sentiment_sha=sentiment_receipt.sha256,
            include_evidence=include_evidence,
        )
        validate_artifact("decision", decision_payload)
        store.persist(ArtifactType.DECISION, decision_payload, run_id="run_20261025T120000Z_provenance")
        context = BybitProductionAccountContext(
            FakeClient(), oms=FakeOms(), breaker=VenueCircuitBreaker(),
            artifact_store=store, now=lambda: NOW,
        )
        return root, context, intent_from(decision_payload)

    def assert_blocked(self, **kwargs):
        root, context, intent = self.build(**kwargs)
        try:
            risk = context.current(intent)
            result = RiskEngine().evaluate(intent, replace(risk, runtime_execution_enabled=True, kill_switch_active=False))
            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("MARKET_DATA_EVIDENCE_UNVERIFIED", result.reason_codes)
        finally:
            root.cleanup()

    def test_valid_evidence_is_accepted_by_final_risk(self):
        root, context, intent = self.build()
        try:
            risk = context.current(intent)
            result = RiskEngine().evaluate(intent, replace(risk, runtime_execution_enabled=True, kill_switch_active=False))
            self.assertEqual(result.status, "APPROVED")
            self.assertTrue(risk.market_evidence.verified)
        finally:
            root.cleanup()

    def test_stale_spot_price_and_rolling_high_block(self):
        old = (NOW - timedelta(minutes=16)).isoformat().replace("+00:00", "Z")
        self.assert_blocked(market=market_snapshot(captured_at=old))

    def test_stale_fear_greed_blocks(self):
        old = (NOW - timedelta(hours=37)).isoformat().replace("+00:00", "Z")
        self.assert_blocked(sentiment=sentiment_snapshot(observed_at=old, retrieved_at=old))

    def test_wrong_venue_or_market_type_blocks(self):
        with self.assertRaises(SchemaValidationError):
            validate_artifact("market_snapshot", market_snapshot(source_exchange="Kraken"))
        with self.assertRaises(SchemaValidationError):
            validate_artifact("market_snapshot", market_snapshot(market_type="linear"))

    def test_price_high_source_mismatch_blocks(self):
        with self.assertRaises(SchemaValidationError):
            validate_artifact("market_snapshot", market_snapshot(same_source=False))

    def test_mismatched_input_digest_blocks(self):
        root, context, intent = self.build()
        try:
            tampered = replace(intent, market_snapshot_sha256="0" * 64)
            risk = context.current(tampered)
            venue = FakeVenue()
            plan = plan_pre_submit(
                intent=tampered,
                context=replace(risk, runtime_execution_enabled=True, kill_switch_active=False),
                risk_engine=RiskEngine(),
                venue=venue,
            )
            self.assertFalse(plan.approved)
            self.assertIn("MARKET_DATA_EVIDENCE_UNVERIFIED", plan.reason_codes)
            self.assertEqual(venue.validated, 0)
        finally:
            root.cleanup()

    def test_missing_evidence_blocks(self):
        self.assert_blocked(include_evidence=False)


if __name__ == "__main__":
    unittest.main()
