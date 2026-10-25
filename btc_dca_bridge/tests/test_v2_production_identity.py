from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from btc_dca_bridge.infrastructure.v2_production import BybitProductionAccountContext
from btc_dca_bridge.private_bybit import (
    AccountInfo,
    ApiCredentialInfo,
    CredentialClassification,
    SpotQuoteAvailability,
)
from btc_dca_bridge.core.models import Asset, Market, OrderIntent, OrderType, Side
from btc_dca_bridge.venues.health import VenueCircuitBreaker


NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


def intent() -> OrderIntent:
    return OrderIntent(
        "intent-identity",
        "adaptive_dca_v1",
        "decision-identity",
        Market(Asset("BTC"), Asset("USDT")),
        Side.BUY,
        OrderType.MARKET,
        Decimal("25"),
        "bybit",
        "client-identity",
        "2026-10-25T12:00:00Z",
    )


class FakeOms:
    def unresolved(self):
        return ()


class FakeClient:
    def __init__(self, *, identity="key-mainnet", environment="mainnet"):
        self.identity = identity
        self.environment = environment

    def credential_info(self):
        return ApiCredentialInfo(
            CredentialClassification.TRADE_CAPABLE,
            False,
            {"Spot": ("SpotTrade",)},
            identity=self.identity,
        )

    def account_info(self):
        return AccountInfo(6, "REGULAR_MARGIN", "OFF", "1")

    def wallet_balances(self):
        return ()

    def spot_quote_availability(self):
        return SpotQuoteAvailability(
            Decimal("100"), "/v5/account/wallet-balance", "availableToWithdraw",
            "UNIFIED", True, "2026-10-25T12:00:00Z",
        )

    def server_time_ms(self):
        return int(NOW.timestamp() * 1000)


def context(client: FakeClient) -> BybitProductionAccountContext:
    return BybitProductionAccountContext(
        client, oms=FakeOms(), breaker=VenueCircuitBreaker(), now=lambda: NOW
    )


class V2ProductionIdentityTests(unittest.TestCase):
    def test_same_account_mode_metadata_different_provider_identities_differ(self):
        first = context(FakeClient(identity="key-one")).current(intent())
        second = context(FakeClient(identity="key-two")).current(intent())
        self.assertNotEqual(
            first.account_identity.fingerprint,
            second.account_identity.fingerprint,
        )

    def test_testnet_identity_is_rejected_by_production_context(self):
        with self.assertRaisesRegex(ValueError, "environment"):
            context(FakeClient(environment="testnet")).current(intent())

    def test_unknown_provider_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            context(FakeClient(identity=None)).current(intent())


if __name__ == "__main__":
    unittest.main()
