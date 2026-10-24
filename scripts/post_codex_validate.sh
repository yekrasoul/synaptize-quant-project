#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/btc_dca_bridge"

echo "== Post-Codex Validation =="

cd "$APP"

echo "[1/6] Locked dependencies"
uv sync --frozen

echo "[2/6] Compile"
uv run python -m compileall -q src tests

echo "[3/6] Unit tests"
uv run python -m unittest discover -s tests -v

echo "[4/6] Canonical repository validation"
uv run python -m btc_dca_bridge validate

cd "$ROOT"

echo "[5/6] Protected execution configuration"
git diff --exit-code -- btc_dca_bridge/config/execution.yaml

echo "[6/6] Ledger integrity"
git diff --exit-code -- btc_dca_bridge/ledger/executions.jsonl

git diff --check

echo
echo "POST-CODEX VALIDATION: PASS"
echo "Production execution state remains unchanged."
