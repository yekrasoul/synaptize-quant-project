import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btc_dca_bridge.quote_limits import (
    QuoteUnitLimitEvidence,
    QuoteUnitLimitPolicy,
    QuoteUnitLimitValidationError,
    PRODUCTION_QUOTE_UNIT_LIMIT_POLICY,
    validate_quote_unit_limit_evidence,
)


class QuoteUnitLimitTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 7, 12, tzinfo=UTC)
        self.policy = QuoteUnitLimitPolicy(frozenset({("/test/limit", "maxQuote")}), max_age=timedelta(minutes=10))

    def evidence(self, **changes):
        values = dict(symbol="BTCUSDT", market_type="spot", side="Buy", order_type="Market", market_unit="quoteCoin", quote_currency="USDT", source_endpoint="/test/limit", source_field="maxQuote", unit="USDT", maximum_quote_usdt=Decimal("100"), authoritative=True, observed_at_utc="2026-10-07T11:59:00Z", conclusion="CONFIRMED")
        values.update(changes)
        return QuoteUnitLimitEvidence(**values)

    def test_production_policy_has_no_approved_source(self):
        self.assertEqual(PRODUCTION_QUOTE_UNIT_LIMIT_POLICY.approved_sources, frozenset())
        self.assertFalse(PRODUCTION_QUOTE_UNIT_LIMIT_POLICY.quote_unit_maximum_required)
        with self.assertRaises(QuoteUnitLimitValidationError):
            validate_quote_unit_limit_evidence(self.evidence(), now=self.now)

    def test_injected_authoritative_quote_source_passes(self):
        self.assertEqual(validate_quote_unit_limit_evidence(self.evidence(), now=self.now, policy=self.policy), Decimal("100"))

    def test_rejects_plain_decimal_base_unit_derived_and_bad_identity(self):
        invalid = (
            Decimal("100"),
            self.evidence(unit="baseCoin"),
            self.evidence(maximum_quote_usdt=None),
            self.evidence(source_endpoint="/v5/market/instruments-info"),
            self.evidence(symbol="ETHUSDT"),
            self.evidence(side="Sell"),
            self.evidence(market_unit="baseCoin"),
            self.evidence(order_type="Limit"),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(QuoteUnitLimitValidationError):
                validate_quote_unit_limit_evidence(value, now=self.now, policy=self.policy)

    def test_rejects_stale_malformed_and_non_authoritative_evidence(self):
        invalid = (
            self.evidence(observed_at_utc="2026-10-07T11:49:59Z"),
            self.evidence(observed_at_utc="not-time"),
            self.evidence(maximum_quote_usdt="NaN"),
            self.evidence(maximum_quote_usdt="Infinity"),
            self.evidence(maximum_quote_usdt="0"),
            self.evidence(authoritative=False),
            self.evidence(conclusion="NOT_EXPOSED"),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(QuoteUnitLimitValidationError):
                validate_quote_unit_limit_evidence(value, now=self.now, policy=self.policy)

    def test_v1_amounts_fit_only_when_authoritative_maximum_is_sufficient(self):
        maximum = validate_quote_unit_limit_evidence(self.evidence(maximum_quote_usdt=Decimal("100")), now=self.now, policy=self.policy)
        for amount in (Decimal("10"), Decimal("25"), Decimal("50"), Decimal("75"), Decimal("100")):
            self.assertLessEqual(amount, maximum)
        self.assertGreater(Decimal("100"), validate_quote_unit_limit_evidence(self.evidence(maximum_quote_usdt=Decimal("100")), now=self.now, policy=self.policy) - Decimal("0.01"))

    def test_v1_boundary_matrix(self):
        for maximum, passing in ((Decimal("9.99"), ()), (Decimal("10"), ("10",)), (Decimal("25"), ("10", "25")), (Decimal("100"), ("10", "25", "50", "75", "100"))):
            with self.subTest(maximum=maximum):
                ceiling = validate_quote_unit_limit_evidence(self.evidence(maximum_quote_usdt=maximum), now=self.now, policy=self.policy)
                for amount in ("10", "25", "50", "75", "100"):
                    self.assertEqual(Decimal(amount) <= ceiling, amount in passing)

    def test_missing_quote_maximum_is_not_required_for_quote_sized_operation(self):
        self.assertFalse(PRODUCTION_QUOTE_UNIT_LIMIT_POLICY.quote_unit_maximum_required)
        self.assertIsNone(QuoteUnitLimitEvidence.from_mapping({
            **self.evidence().to_dict(), "maximum_quote_usdt": None,
            "authoritative": False, "conclusion": "NOT_EXPOSED",
        }).maximum_quote_usdt)


if __name__ == "__main__":
    unittest.main()
