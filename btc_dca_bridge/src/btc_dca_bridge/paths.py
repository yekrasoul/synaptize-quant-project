"""Default paths to canonical, repository-owned artifacts."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "strategy_v1.yaml"
LEDGER_PATH = PROJECT_ROOT / "ledger" / "executions.jsonl"
SCHEMAS_PATH = PROJECT_ROOT / "schemas"
