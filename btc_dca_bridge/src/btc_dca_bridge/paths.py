"""Default paths to canonical, repository-owned artifacts."""

from pathlib import Path

from .project_manifest import load_project_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_MANIFEST_PATH = PROJECT_ROOT / "config" / "project_manifest.yaml"
PROJECT_MANIFEST = load_project_manifest(root=PROJECT_ROOT, manifest_path=PROJECT_MANIFEST_PATH)
CONFIG_PATH = PROJECT_MANIFEST.strategy_config
LEDGER_PATH = PROJECT_MANIFEST.resource("ledger")
PROJECT_CHAT_CONTRACT_PATH = PROJECT_MANIFEST.resource("chat_contract")
MARKET_DATA_CONFIG_PATH = PROJECT_MANIFEST.resource("market_data")
RUNTIME_CONFIG_PATH = PROJECT_MANIFEST.resource("runtime")
EXECUTION_CONFIG_PATH = PROJECT_MANIFEST.resource("execution")
SENTIMENT_CONFIG_PATH = PROJECT_MANIFEST.resource("sentiment")
NOTIFICATIONS_CONFIG_PATH = PROJECT_ROOT / "config" / "notifications.yaml"
PERSISTENCE_CONFIG_PATH = PROJECT_ROOT / "config" / "persistence.yaml"
RESEARCH_CONFIG_PATH = PROJECT_ROOT / "config" / "research.yaml"
SCHEMAS_PATH = PROJECT_ROOT / "schemas"
DATA_PATH = PROJECT_ROOT / "data"
