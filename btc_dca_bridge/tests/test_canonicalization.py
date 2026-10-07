import ast
import unittest
from pathlib import Path

from btc_dca_bridge.cli import _parser
from btc_dca_bridge.market_data.bybit import BYBIT_SOURCE
from btc_dca_bridge.market_data.provider import FallbackMarketDataProvider
from btc_dca_bridge.market_data.tradingview import TRADINGVIEW_SOURCE
from btc_dca_bridge.sentiment.alternative_me import SOURCE as SENTIMENT_SOURCE


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "btc_dca_bridge"


class CanonicalizationTest(unittest.TestCase):
    def test_legacy_collector_mutable_output_and_pending_plan_are_removed(self):
        self.assertFalse((ROOT / "collect_bybit_spot.py").exists())
        self.assertFalse((ROOT / "latest.json").exists())
        self.assertFalse((ROOT / "docs" / "MIGRATION_PLAN.md").exists())

    def test_canonical_runtime_has_no_legacy_import_or_alternate_exchange(self):
        forbidden_text = ("binance", "kucoin", "btcusdt.p", "latest.json", "collect_bybit_spot")
        for path in SOURCE_ROOT.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
            imports = [
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            ] + [
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            ]
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertFalse(any("collect_bybit_spot" in item for item in imports))
                lowered = text.lower()
                for forbidden in forbidden_text:
                    self.assertNotIn(forbidden, lowered)

    def test_canonical_market_source_order_is_unchanged_and_closed(self):
        class Source:
            def __init__(self, source):
                self.source = source

        provider = FallbackMarketDataProvider(
            Source(BYBIT_SOURCE), Source(TRADINGVIEW_SOURCE)
        )
        self.assertEqual(provider.primary.source, "bybit_api")
        self.assertEqual(provider.fallback.source, "tradingview")
        with self.assertRaises(ValueError):
            FallbackMarketDataProvider(Source("other"), Source(TRADINGVIEW_SOURCE))

    def test_alternative_me_remains_the_only_sentiment_runtime_source(self):
        self.assertEqual(SENTIMENT_SOURCE, "alternative_me_crypto_fear_greed")
        sentiment_modules = sorted(
            path.name
            for path in (SOURCE_ROOT / "sentiment").glob("*.py")
            if path.name != "__init__.py"
        )
        self.assertEqual(sentiment_modules, ["alternative_me.py", "models.py"])

    def test_shadow_cli_is_the_only_run_mode_and_has_no_legacy_command(self):
        parser = _parser()
        args = parser.parse_args(["run", "--mode", "shadow"])
        self.assertEqual((args.command, args.mode), ("run", "shadow"))
        help_text = parser.format_help().lower()
        self.assertNotIn("collect_bybit", help_text)
        self.assertNotIn("latest.json", help_text)

    def test_production_docs_state_only_the_canonical_sources(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        project = (ROOT / "docs" / "PROJECT_SPEC.md").read_text(encoding="utf-8")
        shadow = (ROOT / "docs" / "SHADOW_PIPELINE.md").read_text(encoding="utf-8")
        combined = "\n".join((readme, project, shadow)).lower()
        self.assertIn("bybit btcusdt spot", combined)
        self.assertIn("bybit:btcusdt", combined)
        self.assertIn("alternative.me", combined)
        self.assertIn("ledger/executions.jsonl", combined)
        self.assertNotIn("api.binance", combined)
        self.assertNotIn("api.kucoin", combined)
        self.assertNotIn("fallback to binance", combined)
        self.assertNotIn("fallback to kucoin", combined)


if __name__ == "__main__":
    unittest.main()
