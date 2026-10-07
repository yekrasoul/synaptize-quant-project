"""JSON CLI for offline validation, portfolio derivation, and calculation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from .config import load_notification_config, load_operational_config, load_strategy_config, load_execution_config
from .artifacts import ArtifactStore, ArtifactType
from .errors import BtcDcaError
from .engine import calculate_decision
from .ledger import read_executions
from .models import MarketSnapshot
from .paths import CONFIG_PATH, DATA_PATH, LEDGER_PATH
from .portfolio import derive_portfolio
from .production import (
    ProductionRunContext,
    read_json_object,
    run_production_shadow,
    write_json_object,
)
from .schemas import validate_all_schemas, validate_artifact
from .shadow import build_live_shadow_pipeline, format_shadow_output
from .execution import JsonInstrumentMetadataProvider, NoSubmissionEvidence, SubmissionEvidenceStore, make_order_intent, validate_execution_safety
from .private_bybit import BybitPrivateReadClient
from .notifications import TelegramNotifier, TelegramTransport, format_failure_message, format_success_message


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

    run = subparsers.add_parser("run", help="run the read-only shadow pipeline")
    run.add_argument("--mode", choices=("shadow",), default="shadow")
    run.add_argument("--run-id")
    run.add_argument("--run-at", help="injected RFC 3339 UTC run time")
    run.add_argument("--config", type=Path, default=CONFIG_PATH)
    run.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    run.add_argument("--data-root", type=Path, help="explicit local artifact root (tests/developer use)")

    production_context = subparsers.add_parser(
        "production-context", help="derive deterministic production-shadow identity"
    )
    production_context.add_argument("--trigger", choices=("scheduled", "manual"), required=True)
    production_context.add_argument("--process-started-at", required=True)
    production_context.add_argument("--trigger-created-at")
    production_context.add_argument("--trigger-id", required=True)
    production_context.add_argument("--output", type=Path, required=True)
    production_context.add_argument("--github-output", type=Path)

    production = subparsers.add_parser(
        "production-shadow", help="run the canonical scheduled/manual production shadow"
    )
    production.add_argument("--context", type=Path, required=True)
    production.add_argument("--result-json", type=Path, required=True)
    production.add_argument("--config", type=Path, default=CONFIG_PATH)
    production.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    production.add_argument("--data-root", type=Path, help="explicit local artifact root (tests/developer use)")
    production.add_argument("--github-output", type=Path)

    notify = subparsers.add_parser(
        "notify-shadow", help="send Telegram from a structured production result"
    )
    notify.add_argument("--result-json", type=Path, required=True)
    notify.add_argument("--failure-stage")
    notify.add_argument("--failure-category")

    summary = subparsers.add_parser(
        "summarize-shadow", help="write a structured production-shadow job summary"
    )
    summary.add_argument("--result-json", type=Path, required=True)
    summary.add_argument("--summary-file", type=Path, required=True)
    summary.add_argument("--retention-status", required=True)
    plan = subparsers.add_parser("execution-plan", help="plan and validate an order intent; never submits")
    plan.add_argument("--decision-json", type=Path, required=True)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--created-at", default="2026-01-01T00:00:00Z")
    plan.add_argument("--month", required=True)
    plan.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    plan.add_argument("--data-root", type=Path, default=DATA_PATH)
    plan.add_argument("--instrument-metadata", type=Path, help="validated read-only Bybit Spot instrument-info JSON")
    plan.add_argument("--submission-evidence", type=Path, help="read-only submission/reconciliation evidence JSONL")
    private = subparsers.add_parser("private-verify", help="read-only authenticated Bybit verification")
    private.add_argument("--order-link-id", help="exact deterministic client order ID to reconcile")
    return parser


def _run_time(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--run-at must be an RFC 3339 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("--run-at must be an RFC 3339 UTC timestamp")
    return parsed.astimezone(UTC)


def _run_shadow(args: argparse.Namespace):
    run_at = _run_time(args.run_at)
    pipeline = build_live_shadow_pipeline(
        run_at_utc=run_at,
        config_path=args.config,
        ledger_path=args.ledger,
        data_root=args.data_root,
    )
    return pipeline.run(run_at_utc=run_at, run_id=args.run_id)


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
    state = derive_portfolio(
        read_executions(args.ledger), args.month, strategy.monthly_cap_usd
    ).to_dict()
    validate_artifact("portfolio_state", state)
    return state


def _validate(args: argparse.Namespace) -> dict[str, object]:
    schemas = validate_all_schemas()
    strategy = load_strategy_config(args.config)
    executions = read_executions(args.ledger)
    months = sorted({item.executed_at_utc[:7] for item in executions})
    portfolio_states = []
    for month in months:
        state = derive_portfolio(
            executions, month, strategy.monthly_cap_usd
        ).to_dict()
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


def _production_context(args: argparse.Namespace) -> dict[str, object]:
    operational = load_operational_config()
    context = ProductionRunContext.create(
        trigger_type=args.trigger,
        process_started_at_utc=_run_time(args.process_started_at),
        trigger_id=args.trigger_id,
        trigger_created_at_utc=(
            _run_time(args.trigger_created_at) if args.trigger_created_at else None
        ),
    )
    payload = {**context.to_dict(), "runtime_config_version": operational.runtime.config_version}
    write_json_object(args.output, payload)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"run_id={context.run_id}\n")
            handle.write(f"logical_run_at_utc={context.logical_run_at_utc}\n")
            handle.write(f"trigger_type={context.trigger_type}\n")
            handle.write(f"completed_retention_days={operational.persistence.completed_retention_days}\n")
            handle.write(f"partial_retention_days={operational.persistence.partial_retention_days}\n")
    return payload


def _production_shadow(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    context = ProductionRunContext.from_dict(read_json_object(args.context))
    result = run_production_shadow(
        context,
        config_path=args.config,
        ledger_path=args.ledger,
        data_root=args.data_root,
    ).to_dict()
    write_json_object(args.result_json, result)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"status={result['status']}\n")
            handle.write(f"run_id={result['run_id']}\n")
    return result, 0 if result["status"] in {"completed", "already_completed"} else 2


def _notify_shadow(args: argparse.Namespace) -> dict[str, object]:
    outcome = read_json_object(args.result_json)
    config = load_notification_config()
    if outcome.get("status") == "already_completed" and not args.failure_category and config.suppress_duplicate_success:
        outcome["notification_status"] = "suppressed"
        outcome["duplicate_notification_suppressed"] = True
        write_json_object(args.result_json, outcome)
        return outcome
    if args.failure_category or outcome.get("status") == "failed":
        message = format_failure_message(
            outcome,
            stage=args.failure_stage,
            category=args.failure_category,
        )
    elif outcome.get("status") == "completed":
        message = format_success_message(outcome)
    else:
        raise ValueError("structured production result has unsupported status")
    if not config.telegram_enabled:
        outcome["notification_status"] = "disabled"
        write_json_object(args.result_json, outcome)
        return outcome
    token = os.environ.get(config.bot_token_env_var, "")
    chat_id = os.environ.get(config.chat_id_env_var, "")
    try:
        delivery = TelegramNotifier(
            token,
            chat_id,
            transport=TelegramTransport(
                connect_timeout_seconds=config.http.connect_timeout_seconds,
                read_timeout_seconds=config.http.read_timeout_seconds,
                max_attempts=config.http.retry_attempts,
                backoff_seconds=config.http.backoff_seconds,
            ),
        ).send(message)
    except (BtcDcaError, ValueError):
        outcome["notification_status"] = "failed"
        write_json_object(args.result_json, outcome)
        raise
    outcome["notification_status"] = "sent"
    outcome["notification_delivery"] = {
        "channel": "telegram",
        "message_id": delivery.message_id,
        "attempts": delivery.attempts,
    }
    write_json_object(args.result_json, outcome)
    return outcome


def _summarize_shadow(args: argparse.Namespace) -> dict[str, object]:
    outcome = read_json_object(args.result_json)
    lines = [
        "## Production shadow",
        "",
        f"- Run ID: `{outcome.get('run_id', 'unknown')}`",
        f"- Trigger: `{outcome.get('trigger_type', 'unknown')}`",
        f"- Logical slot: `{outcome.get('logical_run_at_utc', 'unknown')}`",
        f"- Status: `{outcome.get('status', 'unknown')}`",
        f"- Retention: `{args.retention_status}`",
        f"- Notification: `{outcome.get('notification_status', 'unknown')}`",
        "- Mode: `SHADOW — NO ORDER EXECUTED`",
    ]
    shadow = outcome.get("shadow_result")
    if isinstance(shadow, dict):
        market = shadow.get("market_snapshot", {})
        sentiment = shadow.get("sentiment_snapshot", {})
        decision = shadow.get("decision", {})
        market_metadata = market.get("market_data_metadata", {})
        source = (
            market_metadata.get("source", "unknown")
            if isinstance(market_metadata, dict)
            else "unknown"
        )
        lines.extend(
            (
                f"- Market source: `{source}`",
                f"- Drawdown: `{decision.get('drawdown_percent', 'unknown')}%`",
                f"- Fear & Greed: `{sentiment.get('value', 'unknown')}`",
                f"- Final purchase: `${decision.get('final_purchase_usd', 'unknown')}`",
            )
        )
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    with args.summary_file.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return {"status": "written", "run_id": outcome.get("run_id")}


def _execution_plan(args: argparse.Namespace) -> dict[str, object]:
    decision = read_json_object(args.decision_json)
    config = load_execution_config()
    intent = make_order_intent(decision, run_id=args.run_id, created_at_utc=args.created_at)
    instrument_provider = None
    if args.instrument_metadata is not None:
        instrument_provider = JsonInstrumentMetadataProvider(json.loads(args.instrument_metadata.read_text(encoding="utf-8")))
    evidence = SubmissionEvidenceStore(args.submission_evidence) if args.submission_evidence else NoSubmissionEvidence()
    validation = validate_execution_safety(intent, decision, ledger_path=args.ledger, calendar_month=args.month,
                                            instrument_provider=instrument_provider, submission_state=evidence,
                                            execution_config=config)
    receipts = [ArtifactStore(args.data_root).persist(ArtifactType.ORDER_INTENT, intent, run_id=args.run_id),
                ArtifactStore(args.data_root).persist(ArtifactType.SAFETY_VALIDATION, validation, run_id=args.run_id)]
    return {"order_intent": intent.to_dict(), "safety_validation": validation.to_dict(),
            "artifact_receipts": [{"artifact_type": r.artifact_type.value, "path": str(r.path), "sha256": r.sha256} for r in receipts],
            "status": validation.status, "message": "NO ORDER EXECUTED"}


def _private_verify(args: argparse.Namespace) -> dict[str, object]:
    client = BybitPrivateReadClient.from_environment()
    credential = client.credential_info()
    account = client.account_info()
    balances = client.wallet_balances()
    rules = client.instrument_rules()
    result: dict[str, object] = {
        "read_only": True,
        "credential": {"classification": credential.classification.value, "read_only": credential.read_only, "permissions": credential.permissions, "identity": credential.identity, "expiry": credential.expiry, "ip_restrictions": credential.ip_restrictions, "key_type": credential.key_type},
        "account": {"unified_margin_status": account.unified_margin_status, "margin_mode": account.margin_mode, "spot_hedging_status": account.spot_hedging_status, "updated_time": account.updated_time},
        "balances": [{"coin": b.coin, "wallet_balance": str(b.wallet_balance), "locked": str(b.locked), "borrow_amount": str(b.borrow_amount), "accrued_interest": str(b.accrued_interest), "usd_value": str(b.usd_value), "has_liability": b.has_liability} for b in balances],
        "instrument": {"quote_minimum": str(rules.quote_minimum), "base_quantity_minimum": str(rules.base_quantity_minimum), "quantity_step": str(rules.quantity_step), "price_tick_size": str(rules.price_tick_size)},
        "message": "READ ONLY — NO ORDER EXECUTED",
    }
    if args.order_link_id:
        order = client.lookup_order(args.order_link_id)
        fills = client.executions(args.order_link_id)
        result["order"] = {"order_link_id": order.order_link_id, "order_id": order.order_id, "state": order.state.value, "raw_status": order.raw_status, "executed_qty": str(order.executed_qty), "executed_value": str(order.executed_value)}
        result["executions"] = [{"exec_qty": str(f.exec_qty), "exec_price": str(f.exec_price), "exec_value": str(f.exec_value), "exec_fee": str(f.exec_fee), "exec_time": f.exec_time, "exec_id": f.exec_id, "order_id": f.order_id, "order_link_id": f.order_link_id} for f in fills]
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        exit_code = 0
        if args.command == "run":
            result = _run_shadow(args)
        elif args.command == "production-context":
            result = _production_context(args)
        elif args.command == "production-shadow":
            result, exit_code = _production_shadow(args)
        elif args.command == "notify-shadow":
            result = _notify_shadow(args)
        elif args.command == "summarize-shadow":
            result = _summarize_shadow(args)
        elif args.command == "execution-plan":
            result = _execution_plan(args)
        elif args.command == "private-verify":
            result = _private_verify(args)
        elif args.command == "calculate":
            result = _calculate(args)
        elif args.command == "portfolio":
            result = _portfolio(args)
        else:
            result = _validate(args)
    except (BtcDcaError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    if args.command == "run":
        print(format_shadow_output(result))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code
