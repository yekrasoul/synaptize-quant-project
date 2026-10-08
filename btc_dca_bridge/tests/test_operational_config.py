from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from btc_dca_bridge.config import (
    ConfigurationError,
    load_market_data_config,
    load_notification_config,
    load_operational_config,
    load_persistence_config,
    load_runtime_config,
    load_sentiment_config,
)
from btc_dca_bridge.production import ProductionRunContext, scheduled_slot
from btc_dca_bridge.shadow import _default_run_id


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parent


class OperationalConfigTests(unittest.TestCase):
    def _copy_with(self, source: str, replacement: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.yaml"
        path.write_text((ROOT / "config" / source).read_text().replace(*replacement), encoding="utf-8")
        return path

    def test_all_committed_operational_config_parses_and_preserves_production_values(self) -> None:
        config = load_operational_config()
        self.assertEqual((config.market_data.primary_provider, config.market_data.fallback_providers), ("bybit_api", ("binance_api", "kucoin_api")))
        self.assertEqual((config.market_data.exchange, config.market_data.market_type, config.market_data.symbol), ("Bybit", "spot", "BTCUSDT"))
        self.assertEqual(config.sentiment.provider, "alternative_me_crypto_fear_greed")
        self.assertEqual(config.sentiment.freshness_max_age_seconds, 36 * 60 * 60)
        self.assertFalse(config.runtime.live_execution_enabled)
        self.assertEqual(config.persistence.digest_algorithm, "sha256")
        self.assertTrue(all(value is False for value in config.research.flags.values()))

    def test_rejects_unapproved_market_identity_and_perpetual_symbol(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_market_data_config(self._copy_with("market_data.yaml", ("market_type: spot", "market_type: linear")))

    def test_rejects_invalid_operational_values(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_sentiment_config(self._copy_with("sentiment.yaml", ("config_version: \"1.0.0\"", "config_version: \"2.0.0\"")))
        with self.assertRaises(ConfigurationError):
            load_runtime_config(self._copy_with("runtime.yaml", ("timezone: Europe/London", "timezone: No/Such_Zone")))
        with self.assertRaises(ConfigurationError):
            load_runtime_config(self._copy_with("runtime.yaml", ("live_execution_enabled: false", "live_execution_enabled: true")))
        with self.assertRaises(ConfigurationError):
            load_notification_config(self._copy_with("notifications.yaml", ("bot_token_env_var: TELEGRAM_BOT_TOKEN", "bot_token: secret")))
        with self.assertRaises(ConfigurationError):
            load_persistence_config(self._copy_with("persistence.yaml", ("digest_algorithm: sha256", "digest_algorithm: md5")))

    def test_runtime_slot_comes_from_validated_config(self) -> None:
        runtime = load_runtime_config()
        started = datetime(2026, 10, 7, 11, 20, tzinfo=UTC)
        self.assertEqual(scheduled_slot(started, runtime_config=runtime), datetime(2026, 10, 7, 6, 23, tzinfo=UTC))
        context = ProductionRunContext.create(trigger_type="scheduled", process_started_at_utc=started, trigger_id="1", runtime_config=runtime)
        self.assertIn("scheduled", context.run_id)

    def test_workflow_matches_runtime_policy(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "production-shadow.yml").read_text(encoding="utf-8")
        runtime = load_runtime_config()
        self.assertIn(f'cron: "{runtime.github_cron_utc}"', workflow)
        self.assertIn(f"timeout-minutes: {runtime.workflow_timeout_minutes}", workflow)
        self.assertIn("completed_retention_days", workflow)
        self.assertIn("partial_retention_days", workflow)
        self.assertNotIn("Binance", workflow)
        self.assertNotIn("KuCoin", workflow)
