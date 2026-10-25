from __future__ import annotations

from decimal import Decimal
import unittest

from btc_dca_bridge.core.models import AccountIdentity
from btc_dca_bridge.execution_v2.shadow import plan_v2_shadow
from btc_dca_bridge.risk.engine import MarketEvidenceBinding, RiskContext


class RecordingVenue:
    name = "bybit"
    def __init__(self): self.validations = 0; self.posts = 0
    def validate_intent(self, _): self.validations += 1
    def prepare_submission(self, intent): return {"symbol": "BTC/USDT", "orderLinkId": intent.client_order_id}
    def submit_once(self, _): self.posts += 1; raise AssertionError("shadow must not POST")


def context():
    return RiskContext("2026-10", Decimal("0"), Decimal("500"), True, False, True, AccountIdentity("bybit", "account-1"), "account-1", Decimal("100"), False, False, venue_health="healthy", market_evidence=MarketEvidenceBinding("decision-x", "market-x", "a" * 64, "sentiment-x", "b" * 64, True, 35))


class V2ShadowParityTests(unittest.TestCase):
    def test_shadow_uses_shared_pre_submit_contract_without_claim_or_post(self):
        venue = RecordingVenue()
        result = plan_v2_shadow({"decision_id": "decision-x", "strategy_id": "adaptive_dca_v1", "final_purchase_usd": 25, "market_snapshot_id": "market-x", "market_snapshot_sha256": "a" * 64, "sentiment_snapshot_id": "sentiment-x", "sentiment_snapshot_sha256": "b" * 64, "fear_greed_index": 35}, run_id="run_20261024T000000Z_shadowv2", created_at_utc="2026-10-24T00:00:00Z", monthly_spend=Decimal("0"), context=context(), venue=venue)
        self.assertTrue(result["would_submit"])
        self.assertTrue(result["no_order_executed"])
        self.assertEqual(result["prepared_submission"]["symbol"], "BTC/USDT")
        self.assertEqual((venue.validations, venue.posts), (1, 0))

    def test_missing_authenticated_context_is_not_fabricated(self):
        result = plan_v2_shadow({"decision_id": "decision-x", "strategy_id": "adaptive_dca_v1", "final_purchase_usd": 25}, run_id="run_20261024T000000Z_shadowv2", created_at_utc="2026-10-24T00:00:00Z", monthly_spend=Decimal("0"))
        self.assertEqual(result["status"], "NOT_VERIFIED")


if __name__ == "__main__": unittest.main()
