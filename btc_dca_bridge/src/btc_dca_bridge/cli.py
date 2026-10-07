"""JSON CLI for offline validation, portfolio derivation, and calculation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from .config import load_notification_config, load_operational_config, load_strategy_config, load_execution_config
from .artifacts import ArtifactStore, ArtifactType
from .errors import ArtifactCorruptError, BtcDcaError
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
from .execution import JsonInstrumentMetadataProvider, NoSubmissionEvidence, SubmissionEvidenceStore, OrderIntent, make_order_intent, validate_execution_safety
from .private_bybit import BybitPostAckReconciler, BybitPrivateReadClient, PrivateBybitError
from .canary import CanaryPreparer
from .availability import PRODUCTION_AVAILABILITY_POLICY
from .live_order import LiveApproval, LiveOrderEngine, SignedBybitSubmissionTransport
from .operations import EXIT_BLOCKED, EXIT_CORRUPT, EXIT_RECONCILIATION, OperationLock, OperationsService
from .notifications import TelegramNotifier, TelegramTransport, format_failure_message, format_success_message
from .readiness import ProductionReadinessService, production_connectivity
from .production_evidence import ProductionEvidenceService, preauthorization_status, _account_fingerprint


# These internal factories are deliberately not CLI options.  They provide a
# narrow dependency seam for offline integration tests while production keeps
# constructing only the reviewed Bybit clients below.
_private_read_client_factory = BybitPrivateReadClient.from_environment
_submission_transport_factory = SignedBybitSubmissionTransport
_post_ack_reconciler_factory = BybitPostAckReconciler


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
    live = subparsers.add_parser("live-submit", help="disabled-by-default Phase 5.3 submission gate")
    live.add_argument("--decision-json", type=Path, required=True)
    canary = subparsers.add_parser("canary-prepare", help="prepare a read-only one-shot canary manifest")
    canary.add_argument("--decision-json", type=Path, required=True)
    canary.add_argument("--run-id", required=True)
    canary.add_argument("--month", required=True)
    canary.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    canary.add_argument("--data-root", type=Path, default=DATA_PATH)
    for name, help_text in (("ops-status", "read-only controlled operations state"), ("ops-plan", "read-only allowed-next-action preflight"), ("ops-health", "read-only operational integrity health")):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--data-root", type=Path, default=DATA_PATH)
        command.add_argument("--ledger", type=Path, default=LEDGER_PATH)
        command.add_argument("--run-id")
        command.add_argument("--json", action="store_true")
    audit = subparsers.add_parser("audit-run", help="read-only immutable artifact-chain audit")
    audit.add_argument("run_id")
    audit.add_argument("--data-root", type=Path, default=DATA_PATH)
    audit.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    audit.add_argument("--json", action="store_true")
    approve = subparsers.add_parser("canary-approve", help="persist a precise five-minute manual approval")
    approve.add_argument("--run-id", required=True); approve.add_argument("--canary-id", required=True)
    approve.add_argument("--manifest-sha", required=True); approve.add_argument("--amount", required=True)
    approve.add_argument("--client-order-id", required=True); approve.add_argument("--payload-fingerprint", required=True)
    approve.add_argument("--data-root", type=Path, default=DATA_PATH); approve.add_argument("--json", action="store_true")
    execute = subparsers.add_parser("canary-execute", help="one exact manual invocation of the guarded engine")
    execute.add_argument("--run-id", required=True); execute.add_argument("--canary-id", required=True); execute.add_argument("--approval-id", required=True)
    execute.add_argument("--manifest-sha", required=True); execute.add_argument("--approval-sha", required=True)
    execute.add_argument("--data-root", type=Path, default=DATA_PATH); execute.add_argument("--ledger", type=Path, default=LEDGER_PATH); execute.add_argument("--month", required=True); execute.add_argument("--json", action="store_true")
    recover = subparsers.add_parser("reconcile-existing", help="no-POST recovery of one exact prior identity")
    recover.add_argument("--run-id", required=True); recover.add_argument("--canary-id", required=True); recover.add_argument("--approval-id", required=True)
    recover.add_argument("--manifest-sha", required=True); recover.add_argument("--approval-sha", required=True)
    recover.add_argument("--data-root", type=Path, default=DATA_PATH); recover.add_argument("--ledger", type=Path, default=LEDGER_PATH); recover.add_argument("--json", action="store_true")
    readiness = subparsers.add_parser("production-readiness", help="read-only production activation readiness gate")
    readiness.add_argument("--data-root", type=Path, default=DATA_PATH); readiness.add_argument("--ledger", type=Path, default=LEDGER_PATH); readiness.add_argument("--json", action="store_true")
    connectivity = subparsers.add_parser("production-connectivity", help="read-only authenticated Bybit connectivity check")
    connectivity.add_argument("--json", action="store_true")
    evidence = subparsers.add_parser("collect-production-evidence", help="collect immutable read-only production evidence")
    evidence.add_argument("--data-root", type=Path, default=DATA_PATH); evidence.add_argument("--ledger", type=Path, default=LEDGER_PATH); evidence.add_argument("--json", action="store_true")
    verify_evidence = subparsers.add_parser("verify-production-evidence", help="verify one immutable production evidence bundle")
    verify_evidence.add_argument("evidence_id"); verify_evidence.add_argument("--data-root", type=Path, default=DATA_PATH); verify_evidence.add_argument("--ledger", type=Path, default=LEDGER_PATH); verify_evidence.add_argument("--json", action="store_true")
    preauth = subparsers.add_parser("preauthorization-status", help="read-only evidence and readiness preauthorization status")
    preauth.add_argument("--data-root", type=Path, default=DATA_PATH); preauth.add_argument("--ledger", type=Path, default=LEDGER_PATH); preauth.add_argument("--json", action="store_true")
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


def _live_submit(args: argparse.Namespace) -> dict[str, object]:
    config = load_execution_config()
    if not config.live_execution_enabled:
        return {"status": "blocked", "reason": "LIVE EXECUTION DISABLED", "message": "NO ORDER SUBMITTED"}
    if config.kill_switch:
        return {"status": "blocked", "reason": "KILL SWITCH ACTIVE", "message": "NO ORDER SUBMITTED"}
    return {"status": "blocked", "reason": "LIVE SUBMISSION REQUIRES CONTROLLED APPROVAL", "message": "NO ORDER SUBMITTED"}


def _canary_prepare(args: argparse.Namespace) -> dict[str, object]:
    decision = read_json_object(args.decision_json)
    try:
        client = BybitPrivateReadClient.from_environment()
    except Exception as exc:
        error_message = str(exc)
        class UnavailableReadClient:
            def __getattr__(self, name):
                def unavailable(*unused_args, **unused_kwargs): raise RuntimeError(f"private read verification unavailable: {error_message}")
                return unavailable
        client = UnavailableReadClient()
    result = CanaryPreparer(artifact_store=ArtifactStore(args.data_root), client=client, availability_policy=PRODUCTION_AVAILABILITY_POLICY).prepare(
        decision, run_id=args.run_id, calendar_month=args.month, ledger_path=args.ledger
    )
    manifest = result.manifest.to_dict()
    status_line = "CANARY READY FOR MANUAL APPROVAL" if manifest["canary_status"] == "READY_FOR_MANUAL_APPROVAL" else "CANARY BLOCKED"
    balances = {"USDT": manifest["wallet_usdt"], "BTC": manifest["wallet_btc"]}
    summary = "\n".join((
        f"Decision ID: {manifest['decision_id']}",
        f"Client order ID: {manifest['client_order_id']}",
        f"BTC market: {manifest['exchange']} {manifest['market_type']} {manifest['symbol']}",
        f"Order side: {manifest['side']}",
        f"Order type: {manifest['order_type']}",
        f"V1 amount: {manifest['approved_amount_usdt']}",
        f"Monthly spent: {manifest['monthly_spent_usd']}",
        f"Remaining monthly budget: {manifest['remaining_budget_usd']}",
        f"USDT available: {balances['USDT']}",
        f"BTC balance: {balances['BTC']}",
        f"Liability detected: {manifest['liability_detected']}",
        f"Credential classification: {manifest['credential_classification']}",
        f"Pre-submission reconciliation: {manifest['pre_submission_state']}",
        f"Manifest expiry: {manifest['expires_at_utc']}",
        f"Payload SHA-256: {manifest['order_payload_fingerprint']}",
        f"Canary status: {manifest['canary_status']}",
        status_line,
        "NO ORDER SUBMITTED",
    ))
    return {"manifest": manifest, "artifact_receipt": {"path": str(result.artifact_receipt.path), "sha256": result.artifact_receipt.sha256}, "summary": summary}


def _ops(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    service = OperationsService(data_root=args.data_root, ledger_path=args.ledger)
    if args.command == "ops-status":
        snapshot = service.snapshot(run_id=args.run_id)
        return snapshot.to_dict(), EXIT_RECONCILIATION if snapshot.reconciliation_required else (EXIT_BLOCKED if snapshot.state == "BLOCKED" else 0)
    if args.command == "ops-plan":
        snapshot = service.snapshot(run_id=args.run_id)
        exit_code = EXIT_RECONCILIATION if snapshot.reconciliation_required else (EXIT_BLOCKED if snapshot.state == "BLOCKED" else 0)
        return {**snapshot.to_dict(), "current_state": snapshot.state, "allowed_next_action": snapshot.allowed_actions[0] if snapshot.allowed_actions else None, "message": "READ ONLY — NO ORDER SUBMITTED"}, exit_code
    if args.command == "ops-health":
        result = service.health()
        return result, EXIT_CORRUPT if result["status"] == "CORRUPT" else (EXIT_RECONCILIATION if result["status"] == "HEALTHY_WITH_UNRESOLVED_RECONCILIATION" else (EXIT_BLOCKED if result["status"] == "BLOCKED" else 0))
    audit = service.audit_run(args.run_id)
    return audit, 0 if audit["status"] == "complete" else EXIT_BLOCKED


def _approval_from_dict(payload: dict[str, object]) -> LiveApproval:
    return LiveApproval(str(payload["decision_id"]), str(payload["order_intent_id"]), str(payload["client_order_id"]), Decimal(str(payload["approved_amount_usdt"])), str(payload["approved_at_utc"]), str(payload["expires_at_utc"]), str(payload["approval_id"]), str(payload["canary_id"]), str(payload["manifest_sha256"]), str(payload["order_payload_fingerprint"]), str(payload["exchange"]), str(payload["market_type"]), str(payload["symbol"]), str(payload["side"]), str(payload["order_type"]), bool(payload["standing_authorization"]))


def _intent_from_dict(payload: dict[str, object]) -> OrderIntent:
    return OrderIntent(str(payload["schema_version"]), str(payload["strategy_id"]), str(payload["strategy_version"]), str(payload["run_id"]), str(payload["decision_id"]), str(payload["order_intent_id"]), str(payload["created_at_utc"]), str(payload["exchange"]), str(payload["market_type"]), str(payload["symbol"]), str(payload["side"]), Decimal(str(payload["quote_amount_usd"])), str(payload["expected_mode"]), str(payload["client_order_id"]), Decimal(str(payload["monthly_spent_before_usd"])), Decimal(str(payload["remaining_budget_before_usd"])), str(payload["safety_validation_status"]), bool(payload["live_execution_requested"]), bool(payload["live_execution_enabled"]), bool(payload.get("no_order_executed", True)))


def _canary_approve(args: argparse.Namespace) -> dict[str, object]:
    store = ArtifactStore(args.data_root)
    manifest, digest = store.find_artifact(ArtifactType.CANARY_MANIFEST, identity_field="canary_id", identity_value=args.canary_id)
    if digest != args.manifest_sha or manifest["client_order_id"] != args.client_order_id or manifest["approved_amount_usdt"] != args.amount or manifest["order_payload_fingerprint"] != args.payload_fingerprint:
        raise ValueError("approval inputs do not bind exactly to the persisted manifest")
    approval = LiveApproval.for_manifest(manifest, digest, now_utc=datetime.now(UTC))
    receipt = store.persist(ArtifactType.LIVE_APPROVAL, approval, run_id=args.run_id)
    OperationsService(data_root=args.data_root, ledger_path=LEDGER_PATH).record_event(action="approval_created", result="persisted", reason="exact manifest-bound manual approval", run_id=args.run_id, artifact_ids={"canary_id": approval.canary_id, "approval_id": approval.approval_id, "client_order_id": approval.client_order_id})
    return {"status": "approval_persisted", "approval_id": approval.approval_id, "approval_sha256": receipt.sha256, "summary": {"exchange": "Bybit", "market": "Spot", "symbol": "BTCUSDT", "side": "Buy", "order_type": "Market", "quote_amount_usdt": str(approval.approved_amount_usdt), "leverage": 0, "canary_id": approval.canary_id, "client_order_id": approval.client_order_id, "manifest_sha256": approval.manifest_sha256, "expires_at_utc": approval.expires_at_utc}}


def _load_exact_execution_artifacts(args: argparse.Namespace) -> tuple[ArtifactStore, dict[str, object], dict[str, object], LiveApproval, str, str, OrderIntent, dict[str, object]]:
    store = ArtifactStore(args.data_root)
    manifest, manifest_sha = store.find_artifact(ArtifactType.CANARY_MANIFEST, identity_field="canary_id", identity_value=args.canary_id)
    approval_payload, approval_sha = store.find_artifact(ArtifactType.LIVE_APPROVAL, identity_field="approval_id", identity_value=args.approval_id)
    if manifest_sha != args.manifest_sha or approval_sha != args.approval_sha:
        raise ValueError("supplied immutable digest does not match persisted artifact")
    approval = _approval_from_dict(approval_payload)
    intent_payload, _ = store.find_artifact(ArtifactType.ORDER_INTENT, identity_field="order_intent_id", identity_value=str(manifest["order_intent_id"]))
    decision, _ = store.find_artifact(ArtifactType.DECISION, identity_field="decision_id", identity_value=str(manifest["decision_id"]))
    return store, manifest, approval_payload, approval, manifest_sha, approval_sha, _intent_from_dict(intent_payload), decision


def _canary_execute(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    store, manifest, _, approval, manifest_sha, approval_sha, intent, decision = _load_exact_execution_artifacts(args)
    config = load_execution_config()
    # Checked-in config blocks before client/transport construction. The
    # guarded engine remains the sole future submission implementation.
    if not config.live_execution_enabled or config.kill_switch or config.order_submission == "not_implemented":
        return {"status": "blocked", "reason": "checked-in production defaults prohibit execution", "message": "NO ORDER SUBMITTED"}, EXIT_BLOCKED
    # The persisted intent documents the fail-safe prepared mode.  The exact
    # immutable identity is retained while the separately approved runtime
    # gate supplies the only execution-mode transition.
    intent = replace(intent, live_execution_enabled=True)
    client = _private_read_client_factory()
    lock = OperationLock(args.data_root, client_order_id=intent.client_order_id, approval_id=approval.approval_id, canary_id=str(manifest["canary_id"]), now=lambda: datetime.now(UTC))
    lock.acquire()
    try:
        engine = LiveOrderEngine(artifact_store=store, availability_policy=PRODUCTION_AVAILABILITY_POLICY)
        result = engine.submit(intent, decision, calendar_month=args.month, ledger_path=args.ledger, execution_config=config, approval=approval, approval_sha256=approval_sha, manifest=manifest, manifest_sha256=manifest_sha, read_client=client, transport=_submission_transport_factory(os.environ.get("BYBIT_API_KEY", ""), os.environ.get("BYBIT_API_SECRET", "")), run_id=args.run_id, post_ack_reconciler=_post_ack_reconciler_factory(client))
    finally:
        lock.release()
    return result.outcome.to_dict(), 0 if result.outcome.outcome_category == "confirmed_execution" else EXIT_RECONCILIATION


def _reconcile_existing(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    store, manifest, _, approval, manifest_sha, approval_sha, intent, _ = _load_exact_execution_artifacts(args)
    client = _private_read_client_factory()
    lock = OperationLock(args.data_root, client_order_id=intent.client_order_id, approval_id=approval.approval_id, canary_id=str(manifest["canary_id"]), now=lambda: datetime.now(UTC))
    lock.acquire()
    try:
        result = LiveOrderEngine(artifact_store=store).reconcile_existing(intent, ledger_path=args.ledger, approval=approval, approval_sha256=approval_sha, manifest=manifest, manifest_sha256=manifest_sha, run_id=args.run_id, post_ack_reconciler=_post_ack_reconciler_factory(client))
    finally:
        lock.release()
    return result.outcome.to_dict(), 0 if result.outcome.outcome_category == "confirmed_execution" else EXIT_RECONCILIATION


def _production_readiness(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    result = ProductionReadinessService(data_root=args.data_root, ledger_path=args.ledger).evaluate()
    return result, 0 if result["status"] == "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION" else 2


def _production_connectivity() -> tuple[dict[str, object], int]:
    result = production_connectivity()
    if result["status"] == "READS_FAILED":
        return result, 5
    if result["status"] != "READS_OK":
        return result, 4
    return result, 0


def _evidence_service(args: argparse.Namespace) -> ProductionEvidenceService:
    return ProductionEvidenceService(data_root=args.data_root, ledger_path=args.ledger, client_factory=_private_read_client_factory)


def _collect_production_evidence(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    bundle, receipt = _evidence_service(args).collect()
    result = {"status": bundle["status"], "evidence_id": bundle["evidence_id"], "evidence_sha256": receipt.sha256, "path": str(receipt.path), "real_money_authorization": bundle["real_money_authorization"], "message": "READ ONLY — NO ORDER SUBMITTED"}
    return result, 0 if bundle["status"] != "EVIDENCE_INCOMPLETE" else 4


def _verify_production_evidence(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    service = _evidence_service(args)
    try:
        client = _private_read_client_factory()
        current_fingerprint, _ = _account_fingerprint(client)
    except Exception as exc:
        return {"status": "ACCOUNT_IDENTITY_UNAVAILABLE", "evidence_id": args.evidence_id, "reason": str(exc)}, 4
    if current_fingerprint is None:
        return {"status": "ACCOUNT_IDENTITY_UNAVAILABLE", "evidence_id": args.evidence_id, "reason": "current account identity could not be proven"}, 4
    result = service.verify(args.evidence_id, current_account_fingerprint=current_fingerprint)
    code = 0 if result["status"] in {"VALID_NOT_READY", "VALID_READY_FOR_SEPARATE_AUTHORIZATION"} else (EXIT_CORRUPT if result["status"] == "CORRUPT" else EXIT_BLOCKED)
    return result, code


def _preauthorization_status(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    result = preauthorization_status(_evidence_service(args))
    return result, 0 if result["status"] != "BLOCKED" else EXIT_BLOCKED


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
        elif args.command == "live-submit":
            result = _live_submit(args)
        elif args.command == "canary-prepare":
            result = _canary_prepare(args)
        elif args.command in {"ops-status", "ops-plan", "ops-health", "audit-run"}:
            result, exit_code = _ops(args)
        elif args.command == "canary-approve":
            result = _canary_approve(args)
        elif args.command == "canary-execute":
            result, exit_code = _canary_execute(args)
        elif args.command == "reconcile-existing":
            result, exit_code = _reconcile_existing(args)
        elif args.command == "production-readiness":
            result, exit_code = _production_readiness(args)
        elif args.command == "production-connectivity":
            result, exit_code = _production_connectivity()
        elif args.command == "collect-production-evidence":
            result, exit_code = _collect_production_evidence(args)
        elif args.command == "verify-production-evidence":
            result, exit_code = _verify_production_evidence(args)
        elif args.command == "preauthorization-status":
            result, exit_code = _preauthorization_status(args)
        elif args.command == "calculate":
            result = _calculate(args)
        elif args.command == "portfolio":
            result = _portfolio(args)
        else:
            result = _validate(args)
    except ArtifactCorruptError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return EXIT_CORRUPT
    except PrivateBybitError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 4
    except (BtcDcaError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    if args.command == "run":
        print(format_shadow_output(result))
    elif args.command == "canary-prepare":
        print(result["summary"])
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code
