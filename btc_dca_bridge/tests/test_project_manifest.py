import tempfile
import unittest
from pathlib import Path

import yaml

from btc_dca_bridge.config import load_active_strategy_config
from btc_dca_bridge.errors import ConfigurationError
from btc_dca_bridge.paths import PROJECT_MANIFEST_PATH, PROJECT_ROOT
from btc_dca_bridge.project_manifest import load_project_manifest


class ProjectManifestTests(unittest.TestCase):
    def test_manifest_declares_existing_canonical_resources(self):
        manifest = load_project_manifest()
        self.assertEqual(manifest.strategy_id, "btc_adaptive_dca_v1")
        self.assertEqual(manifest.strategy_version, "1.0.0")
        self.assertTrue(manifest.strategy_config.is_file())
        self.assertTrue(manifest.resource("ledger").is_file())

    def test_normal_loader_resolves_manifest_strategy(self):
        config = load_active_strategy_config()
        self.assertEqual((config.strategy_id, config.strategy_version), ("btc_adaptive_dca_v1", "1.0.0"))
        from btc_dca_bridge.execution import APPROVED_STRATEGY, APPROVED_VERSION
        self.assertEqual((APPROVED_STRATEGY, APPROVED_VERSION), (config.strategy_id, config.strategy_version))

    def test_traversal_and_absolute_paths_fail_closed(self):
        original = yaml.safe_load(PROJECT_MANIFEST_PATH.read_text())
        for bad in ("../outside.yaml", str((PROJECT_ROOT / "config/strategy_v1.yaml").resolve())):
            with self.subTest(path=bad), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "config").mkdir()
                (root / "schemas").mkdir()
                (root / "schemas/project_manifest.schema.json").write_text((PROJECT_ROOT / "schemas/project_manifest.schema.json").read_text())
                sample = {**original, "active_strategy": {**original["active_strategy"], "config_path": bad}}
                manifest_path = root / "config/project_manifest.yaml"
                manifest_path.write_text(yaml.safe_dump(sample))
                with self.assertRaises(ConfigurationError):
                    load_project_manifest(root=root, manifest_path=manifest_path)

    def test_unsupported_strategy_fails_closed(self):
        original = yaml.safe_load(PROJECT_MANIFEST_PATH.read_text())
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir(); (root / "docs").mkdir(); (root / "ledger").mkdir(); (root / "schemas").mkdir()
            for source in ("config/strategy_v1.yaml", "config/market_data.yaml", "config/runtime.yaml", "config/execution.yaml", "config/sentiment.yaml", "docs/PROJECT_CHAT_CONTRACT.md"):
                dest = root / source; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes((PROJECT_ROOT / source).read_bytes())
            (root / "ledger/executions.jsonl").write_text("")
            (root / "schemas/project_manifest.schema.json").write_bytes((PROJECT_ROOT / "schemas/project_manifest.schema.json").read_bytes())
            sample = {**original, "active_strategy": {**original["active_strategy"], "id": "btc_adaptive_dca_v2", "version": "2.0.0"}}
            manifest_path = root / "config/project_manifest.yaml"; manifest_path.write_text(yaml.safe_dump(sample))
            with self.assertRaisesRegex(ConfigurationError, "unsupported active strategy"):
                load_active_strategy_config(root=root, manifest_path=manifest_path)

    def test_manifest_strategy_identity_must_match_config_identity(self):
        original = yaml.safe_load(PROJECT_MANIFEST_PATH.read_text())
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for source in ("config/strategy_v1.yaml", "config/market_data.yaml", "config/runtime.yaml", "config/execution.yaml", "config/sentiment.yaml", "docs/PROJECT_CHAT_CONTRACT.md"):
                dest = root / source; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes((PROJECT_ROOT / source).read_bytes())
            (root / "ledger/executions.jsonl").parent.mkdir(parents=True, exist_ok=True); (root / "ledger/executions.jsonl").write_text("")
            (root / "schemas").mkdir(); (root / "schemas/project_manifest.schema.json").write_bytes((PROJECT_ROOT / "schemas/project_manifest.schema.json").read_bytes())
            config_path = root / "config/strategy_v1.yaml"
            config_path.write_text(config_path.read_text().replace("btc_adaptive_dca_v1", "different_strategy"))
            manifest_path = root / "config/project_manifest.yaml"; manifest_path.write_text(yaml.safe_dump(original))
            with self.assertRaises(ConfigurationError):
                load_active_strategy_config(root=root, manifest_path=manifest_path)
