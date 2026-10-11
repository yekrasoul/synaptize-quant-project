import unittest

from btc_dca_bridge.errors import SchemaValidationError
from btc_dca_bridge.schemas import validate_artifact


def legacy_payload():
    return {
        "schema_version": "5.1.0",
        "strategy_id": "btc_adaptive_dca_v1",
        "strategy_version": "1.0.0",
        "run_id": "run_20261010T215657Z_canary_persistfix",
        "decision_id": "decision_59489645a526e3785d237300",
        "order_intent_id": (
            "intent_decision_59489645a526e3785d237300_"
            "run_20261010T215657Z_canary_persistfix"
        ),
        "created_at_utc": "2026-10-10T20:40:00Z",
        "exchange": "Bybit",
        "market_type": "spot",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "quote_amount_usd": "10",
        "expected_mode": "recommendation_only",
        "client_order_id": "dca-64a176946584a354e18e5fc973dd4eb7",
        "monthly_spent_before_usd": "95",
        "remaining_budget_before_usd": "405",
        "safety_validation_status": "pending",
        "live_execution_requested": False,
        "live_execution_enabled": False,
        "no_order_executed": True,
    }


class OrderIntentSchemaCompatibilityTests(unittest.TestCase):
    def test_legacy_5_1_0_without_calendar_month_remains_valid(self):
        validate_artifact("order_intent", legacy_payload())

    def test_5_1_1_requires_calendar_month(self):
        payload = legacy_payload()
        payload["schema_version"] = "5.1.1"

        with self.assertRaises(SchemaValidationError):
            validate_artifact("order_intent", payload)

    def test_5_1_1_with_calendar_month_is_valid(self):
        payload = legacy_payload()
        payload["schema_version"] = "5.1.1"
        payload["calendar_month"] = "2026-10"

        validate_artifact("order_intent", payload)


if __name__ == "__main__":
    unittest.main()
