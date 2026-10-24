from __future__ import annotations

import unittest
from decimal import Decimal

from btc_dca_bridge.execution_v2.shadow import plan_v2_shadow


class V2ShadowParityTests(unittest.TestCase):
    def test_shadow_exercises_mapping_and_risk_without_claim_or_post(self):
        result = plan_v2_shadow({"decision_id": "decision-x", "strategy_id": "adaptive_dca_v1", "final_purchase_usd": 25}, run_id="run_20261024T000000Z_shadowv2", created_at_utc="2026-10-24T00:00:00Z", monthly_spend=Decimal("0"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(result["no_order_executed"])
        self.assertEqual(result["ccxt_mapping"]["params"], {"marketUnit": "quoteCoin", "isLeverage": 0, "orderLinkId": result["client_order_id"]})


if __name__ == "__main__": unittest.main()
