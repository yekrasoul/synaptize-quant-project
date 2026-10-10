#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/btc_dca_bridge"

EXPECTED_LEDGER_SHA="c436b9dfa7c669ce2f463a155b3dc7aa6f10af35efe183bb61531999dd4ec920"

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

echo "[5/6] Protected execution surfaces"
git diff --exit-code -- btc_dca_bridge/config/execution.yaml
git diff --exit-code -- btc_dca_bridge/config/active_strategy.json
git diff --exit-code -- .github/workflows/production-shadow.yml

echo "[6/6] Ledger integrity"
actual_ledger_sha="$(sha256sum btc_dca_bridge/ledger/executions.jsonl | awk '{print $1}')"

if [[ "$actual_ledger_sha" != "$EXPECTED_LEDGER_SHA" ]]; then
  echo "ERROR: ledger SHA mismatch" >&2
  echo "expected: $EXPECTED_LEDGER_SHA" >&2
  echo "actual:   $actual_ledger_sha" >&2
  exit 1
fi

git diff --check

echo
echo "POST-CODEX VALIDATION: PASS"
echo "Production execution state remains unchanged."
