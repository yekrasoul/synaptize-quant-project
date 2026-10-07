"""Default paths to canonical, repository-owned artifacts."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "strategy_v1.yaml"
MARKET_DATA_CONFIG_PATH = PROJECT_ROOT / "config" / "market_data.yaml"
SENTIMENT_CONFIG_PATH = PROJECT_ROOT / "config" / "sentiment.yaml"
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "config" / "runtime.yaml"
NOTIFICATIONS_CONFIG_PATH = PROJECT_ROOT / "config" / "notifications.yaml"
PERSISTENCE_CONFIG_PATH = PROJECT_ROOT / "config" / "persistence.yaml"
EXECUTION_CONFIG_PATH = PROJECT_ROOT / "config" / "execution.yaml"
RESEARCH_CONFIG_PATH = PROJECT_ROOT / "config" / "research.yaml"
LEDGER_PATH = PROJECT_ROOT / "ledger" / "executions.jsonl"
SCHEMAS_PATH = PROJECT_ROOT / "schemas"
DATA_PATH = PROJECT_ROOT / "data"
