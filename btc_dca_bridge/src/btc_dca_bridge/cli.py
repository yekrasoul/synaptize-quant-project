"""JSON CLI for offline validation, portfolio derivation, and calculation."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from .config import load_strategy_config
from .errors import BtcDcaError
from .engine import calculate_decision
from .ledger import read_executions
from .models import MarketSnapshot
from .paths import CONFIG_PATH, LEDGER_PATH
from .portfolio import derive_portfolio
from .schemas import validate_all_schemas, validate_artifact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m btc_dca_bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate config, schemas, and ledger")
    validate.add_argument("--config", type=Path, default=CONFIG_PATH)
    validate.add_argument("--ledger", type=Path, default=LEDGER_PATH)

    portfolio = subparsers.add_parser("portfolio", help="derive portfolio from the ledger")
    portfolio.add_argument("--month", required=True, help="calendar month in YYYY-MM form")
    portfolio.add_argument("--config", type=Path, default=CONFIG_PATH)
    portfolio.add_argument("--ledger", type=Path, default=LEDGER_PATH)

    calculate = subparsers.add_parser("calculate", help="calculate an offline V1 decision")
    calculate.add_argument("--price", required=True)
    calculate.add_argument("--high-7d", required=True)
    calculate.add_argument("--fear-greed", required=True, type=int)
    calculate.add_argument("--monthly-spent", required=True)
    calculate.add_argument("--config", type=Path, default=CONFIG_PATH)
    calculate.add_argument("--snapshot-id", default="market_cli_input")
    calculate.add_argument("--captured-at", default="1970-01-08T00:00:00Z")
    return parser


def _calculate(args: argparse.Namespace) -> dict[str, object]:
    snapshot = MarketSnapshot(
        schema_version="1.0.0",
        snapshot_id=args.snapshot_id,
        captured_at_utc=args.captured_at,
        source_exchange="Bybit",
        market_type="spot",
        symbol="BTCUSDT",
        current_price_usdt=args.price,
        rolling_7d_high_usdt=args.high_7d,
        window_start_utc="1970-01-01T00:00:00Z",
        window_end_utc="1970-01-08T00:00:00Z",
        same_source_price_and_high=True,
        full_168h_coverage=True,
        fresh=True,
    )
    validate_artifact("market_snapshot", snapshot.to_dict())
    decision = calculate_decision(
        snapshot,
        args.fear_greed,
        args.monthly_spent,
        load_strategy_config(args.config),
    ).to_dict()
    validate_artifact("decision", decision)
    return decision


def _portfolio(args: argparse.Namespace) -> dict[str, object]:
    strategy = load_strategy_config(args.config)
    return derive_portfolio(
        read_executions(args.ledger), args.month, strategy.monthly_cap_usd
    ).to_dict()


def _validate(args: argparse.Namespace) -> dict[str, object]:
    schemas = validate_all_schemas()
    strategy = load_strategy_config(args.config)
    executions = read_executions(args.ledger)
    months = sorted({item.executed_at_utc[:7] for item in executions})
    portfolio_states = []
    as_of = max(
        (item.executed_at_utc for item in executions),
        default=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    for month in months:
        summary = derive_portfolio(executions, month, strategy.monthly_cap_usd)
        def number(value):
            return int(value) if value == value.to_integral_value() else float(value)

        state = {
            "schema_version": "1.0.0",
            "as_of_utc": as_of,
            "calendar_month": month,
            "monthly_spent_usd": number(summary.monthly_confirmed_usd_deployed),
            "monthly_remaining_usd": number(summary.remaining_monthly_budget_usd),
            "executions_count": summary.confirmed_execution_count,
            "derived_from_execution_ids": list(summary.derived_from_execution_ids),
        }
        validate_artifact("portfolio_state", state)
        portfolio_states.append(month)
    return {
        "status": "valid",
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy.strategy_version,
        "schemas": schemas,
        "execution_records": len(executions),
        "validated_portfolio_months": portfolio_states,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "calculate":
            result = _calculate(args)
        elif args.command == "portfolio":
            result = _portfolio(args)
        else:
            result = _validate(args)
    except (BtcDcaError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
