"""V1 adapter: preserve DCA economics while giving it a strategy boundary."""
from __future__ import annotations

from decimal import Decimal

from ...config import StrategyConfig
from ...engine import calculate_decision
from ...models import MarketSnapshot, StrategyDecision


class AdaptiveDcaStrategy:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def decide(self, snapshot: MarketSnapshot, fear_greed_index: int, monthly_spend: Decimal) -> StrategyDecision:
        # The legacy calculator is pure.  This wrapper is intentionally a
        # behavior-preserving migration seam, not a second economic model.
        return calculate_decision(snapshot, fear_greed_index, monthly_spend, self.config)
