import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import yaml

from btc_dca_bridge.config import load_strategy_config
from btc_dca_bridge.engine import calculate_decision, calculate_drawdown
from btc_dca_bridge.errors import ConfigurationError, InputValidationError
from btc_dca_bridge.models import MarketSnapshot
from btc_dca_bridge.paths import CONFIG_PATH
from btc_dca_bridge.schemas import validate_artifact


def snapshot_for_drawdown(percent: str) -> MarketSnapshot:
    high = Decimal("100000")
    price = high * (Decimal(1) + Decimal(percent) / Decimal(100))
    return MarketSnapshot(
        schema_version="1.0.0",
        snapshot_id="market_test",
        captured_at_utc="2026-10-06T12:00:00Z",
        source_exchange="Bybit",
        market_type="spot",
        symbol="BTCUSDT",
        current_price_usdt=price,
        rolling_7d_high_usdt=high,
        window_start_utc="2026-09-29T12:00:00Z",
        window_end_utc="2026-10-06T12:00:00Z",
        same_source_price_and_high=True,
        full_168h_coverage=True,
        fresh=True,
    )


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.strategy = load_strategy_config()

    def decision(self, drawdown="0", fear_greed=51, spent="0"):
        return calculate_decision(
            snapshot_for_drawdown(drawdown), fear_greed, spent, self.strategy
        ).to_dict()

    def test_drawdown_boundary_semantics(self):
        cases = (
            ("0", 10),
            ("-5", 10),
            ("-5.0001", 25),
            ("-10", 25),
            ("-10.0001", 50),
            ("-15", 50),
            ("-15.0001", 75),
            ("-25", 75),
            ("-25.0001", 100),
        )
        for drawdown, expected in cases:
            with self.subTest(drawdown=drawdown):
                self.assertEqual(self.decision(drawdown)["base_allocation_usd"], expected)

    def test_drawdown_formula(self):
        self.assertEqual(calculate_drawdown("80000", "100000"), Decimal("-20.0"))

    def test_fear_greed_boundary_semantics(self):
        cases = (
            (20, 1.50),
            (21, 1.30),
            (35, 1.30),
            (36, 1.15),
            (50, 1.15),
            (51, 1.00),
            (65, 1.00),
            (66, 0.85),
            (75, 0.85),
            (76, 0.70),
            (90, 0.70),
            (91, 0.50),
        )
        for index, expected in cases:
            with self.subTest(index=index):
                self.assertEqual(self.decision(fear_greed=index)["sentiment_multiplier"], expected)

    def test_spent_zero(self):
        decision = self.decision(spent="0")
        self.assertEqual(decision["remaining_budget_before_usd"], 500)
        self.assertEqual(decision["final_purchase_usd"], 10)

    def test_spent_490_clips_to_ten(self):
        self.assertEqual(self.decision("-25.1", spent="490")["final_purchase_usd"], 10)

    def test_spent_491_returns_zero(self):
        decision = self.decision("-25.1", spent="491")
        self.assertEqual(decision["final_purchase_usd"], 0)
        self.assertEqual(decision["status"], "monthly_cap_reached")

    def test_spent_500_returns_zero(self):
        self.assertEqual(self.decision(spent="500")["final_purchase_usd"], 0)

    def test_allocation_above_remaining_budget_is_clipped(self):
        self.assertEqual(self.decision("-25.1", spent="470")["final_purchase_usd"], 30)

    def test_remaining_budget_below_minimum_returns_zero(self):
        self.assertEqual(self.decision(spent="490.01")["final_purchase_usd"], 0)

    def test_fractional_budget_489_50_clips_to_10(self):
        self.assertEqual(self.decision("-6", spent="489.50")["final_purchase_usd"], 10)

    def test_fractional_budget_490_00_clips_to_10(self):
        self.assertEqual(self.decision("-6", spent="490.00")["final_purchase_usd"], 10)

    def test_fractional_budget_490_25_returns_zero(self):
        self.assertEqual(self.decision("-6", spent="490.25")["final_purchase_usd"], 0)

    def test_fractional_budget_475_40_floors_clip_to_24(self):
        self.assertEqual(self.decision("-6", spent="475.40")["final_purchase_usd"], 24)

    def test_fractional_budget_474_50_keeps_25(self):
        self.assertEqual(self.decision("-6", spent="474.50")["final_purchase_usd"], 25)

    def test_sentiment_cannot_reduce_purchase_below_minimum(self):
        decision = self.decision("0", fear_greed=100)
        self.assertEqual(decision["calculated_allocation_usd"], 10)

    def test_nearest_whole_usd_uses_conventional_half_up_rounding(self):
        decision = self.decision("-11", fear_greed=36)
        self.assertEqual(decision["calculated_allocation_usd"], 58)

    def test_exact_half_dollar_ties_always_round_up(self):
        cases = (
            ("-6", 91, 13),   # 25 * 0.50 = 12.50
            ("-16", 76, 53),  # 75 * 0.70 = 52.50
        )
        for drawdown, fear_greed, expected in cases:
            with self.subTest(drawdown=drawdown, fear_greed=fear_greed):
                self.assertEqual(
                    self.decision(drawdown, fear_greed)["calculated_allocation_usd"],
                    expected,
                )

    def test_repeatability(self):
        first = self.decision("-12.345", fear_greed=30, spent="220.25")
        second = self.decision("-12.345", fear_greed=30, spent="220.25")
        self.assertEqual(first, second)

    def test_generated_decision_conforms_to_schema(self):
        validate_artifact("decision", self.decision("-12", fear_greed=30, spent="220"))

    def test_market_snapshot_conforms_to_schema(self):
        validate_artifact("market_snapshot", snapshot_for_drawdown("-12").to_dict())

    def test_rejects_non_positive_price(self):
        for value in (Decimal("0"), Decimal("-1")):
            snap = snapshot_for_drawdown("0")
            snap = MarketSnapshot(**{**snap.__dict__, "current_price_usdt": value})
            with self.subTest(value=value), self.assertRaises(InputValidationError):
                calculate_decision(snap, 30, 0, self.strategy)

    def test_rejects_non_positive_high(self):
        for value in (Decimal("0"), Decimal("-1")):
            snap = snapshot_for_drawdown("0")
            snap = MarketSnapshot(**{**snap.__dict__, "rolling_7d_high_usdt": value})
            with self.subTest(value=value), self.assertRaises(InputValidationError):
                calculate_decision(snap, 30, 0, self.strategy)

    def test_rejects_price_above_rolling_high(self):
        snap = snapshot_for_drawdown("0")
        snap = MarketSnapshot(**{**snap.__dict__, "current_price_usdt": Decimal("100001")})
        with self.assertRaisesRegex(InputValidationError, "cannot exceed"):
            calculate_decision(snap, 30, 0, self.strategy)

    def test_rejects_invalid_fear_greed(self):
        for value in (-1, 101, 1.5, True):
            with self.subTest(value=value), self.assertRaises(InputValidationError):
                calculate_decision(snapshot_for_drawdown("0"), value, 0, self.strategy)

    def test_rejects_invalid_monthly_spend(self):
        for value in ("-0.01", "500.01", "NaN", "Infinity", True, "invalid"):
            with self.subTest(value=value), self.assertRaises(InputValidationError):
                calculate_decision(snapshot_for_drawdown("0"), 30, value, self.strategy)


class ConfigurationTest(unittest.TestCase):
    def load_modified(self, mutate):
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        mutate(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            return load_strategy_config(path)

    def test_rejects_unexpected_strategy_identity(self):
        with self.assertRaises(ConfigurationError):
            self.load_modified(lambda data: data["strategy"].update(id="other"))

    def test_rejects_unexpected_strategy_version(self):
        with self.assertRaises(ConfigurationError):
            self.load_modified(lambda data: data["strategy"].update(version="2.0.0"))

    def test_rejects_malformed_drawdown_condition(self):
        def mutate(data):
            data["drawdown"]["base_purchase_bands"][0]["condition"] = "run arbitrary code"

        with self.assertRaises(ConfigurationError):
            self.load_modified(mutate)

    def test_rejects_gapped_sentiment_bands(self):
        def mutate(data):
            data["sentiment"]["multiplier_bands"][0]["max_index"] = 19

        with self.assertRaises(ConfigurationError):
            self.load_modified(mutate)

    def test_rejects_missing_fractional_clip_policy(self):
        def mutate(data):
            del data["calculation_policy"][
                "when_calculated_allocation_exceeds_remaining_budget"
            ]

        with self.assertRaises(ConfigurationError):
            self.load_modified(mutate)

    def test_rejects_noncanonical_rounding_tie_breaking(self):
        def mutate(data):
            data["calculation_policy"]["rounding_tie_breaking"] = "ROUND_HALF_EVEN"

        with self.assertRaises(ConfigurationError):
            self.load_modified(mutate)


if __name__ == "__main__":
    unittest.main()
