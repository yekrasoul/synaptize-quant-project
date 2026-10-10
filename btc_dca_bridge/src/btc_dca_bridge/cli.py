"""JSON CLI for offline validation, portfolio derivation, and calculation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from .config import load_notification_config, load_operational_config, load_strategy_config, load_execution_config
from .artifacts import ArtifactStore, ArtifactType
from .errors import ArtifactCorruptError, BtcDcaError
from .engine import calculate_decision
from .ledger import confirmed_executions, read_executions
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
from .availability import PRODUCTION_AVAILABILITY_POLICY, validate_spot_quote_availability
from .live_order import LiveApproval, LiveOrderEngine, SignedBybitSubmissionTransport, SubmissionResult
from .real_money_authorization import authorize_persisted_approval, read_authorization_status, artifact_sha256
from .live_activation import create_execution_activation, read_activation_status
from .production_enablement import ControlledProductionExecutor, gate_d, prepare_production_enablement, read_enablement_status
from .live_readiness import (
    ORDER_CONTRACT,
    LiveExecutionApproval,
    canonical_fingerprint,
    execution_safety_config_fingerprint,
    production_contract_fingerprint,
)
from .runtime_execution import derive_ephemeral_execution_policy
from .errors import ArtifactNotFoundError
from .final_readiness import final_readiness_audit
from .branch_guardian import classify_branch, write_result
from .quote_limits import PRODUCTION_QUOTE_UNIT_LIMIT_POLICY
from .blocked_production import active_production_blockers, contract_status
from .operations import EXIT_BLOCKED, EXIT_CORRUPT, EXIT_RECONCILIATION, OperationLock, OperationsService
from .notifications import TelegramNotifier, TelegramTransport, format_failure_message, format_success_message
from .readiness import ProductionReadinessService, production_connectivity
from .canary_readiness import bybit_canary_readiness
from .production_evidence import ProductionEvidenceService, preauthorization_status, _account_fingerprint
from .readiness_checks import project_gate_d_evidence
from .ttl_policy import (
    CANARY_MANIFEST_TTL,
    EXECUTION_ACTIVATION_TTL,
    LIVE_EXECUTION_APPROVAL_TTL,
    PRODUCTION_ENABLEMENT_TTL,
    REAL_MONEY_AUTHORIZATION_TTL,
)
from .production_status import ProductionStatusService
from .ledger_gateway import LedgerGateway


# These internal factories are deliberately not CLI options.  They provide a
# narrow dependency seam for offline integration tests while production keeps
# constructing only the reviewed Bybit clients below.
_private_read_client_factory = BybitPrivateReadClient.from_environment
_submission_transport_factory = SignedBybitSubmissionTransport
_post_ack_reconciler_factory = BybitPostAckReconciler
_production_status_service_factory = ProductionStatusService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="btc-dca")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate config, schemas, and ledger")
    validate.add_argument("--config", type=Path, default=CONFIG_PATH)
    validate.add_argument("--ledger", type=Path, default=LEDGER_PATH)

    portfolio = subparsers.add_parser("portfolio", help="derive portfolio from the ledger")
    portfolio.add_argument("--month", required=True, help="calendar month in YYYY-MM form")
    portfolio.add_argument("--config", type=Path, default=CONFIG_PATH)
    portfolio.add_argument("--ledger", type=Path, default=LEDGER_PATH)

    ledger_state = subparsers.add_parser("ledger-state", help="derive active ledger state without mutation")
    ledger_state.add_argument("--month", required=True)
    ledger_state.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ledger_state.add_argument("--json", action="store_true")

    ledger_record = subparsers.add_parser("ledger-record", help="record an explicitly user-confirmed manual execution")
    _add_ledger_execution_fields(ledger_record)
    ledger_record.add_argument("--execution-id")
    ledger_record.add_argument("--distinct-execution-intent", action="store_true")

    ledger_correct = subparsers.add_parser("ledger-correct", help="append a correction to an explicit active execution")
    _add_ledger_execution_fields(ledger_correct)
    ledger_correct.add_argument("--target-execution-id", required=True)
    ledger_correct.add_argument("--execution-id")

    ledger_cancel = subparsers.add_parser("ledger-cancel", help="append a cancellation for an explicit active execution")
    ledger_cancel.add_argument("--target-execution-id", required=True)
    ledger_cancel.add_argument("--cancelled-at", required=True)
    ledger_cancel.add_argument("--source", default="Codex CLI")
    ledger_cancel.add_argument("--note", required=True)
    ledger_cancel.add_argument("--execution-id")
    ledger_cancel.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ledger_cancel.add_argument("--month", help="derive post-write state for this month")
    ledger_cancel.add_argument("--json", action="store_true")

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
    live_readiness = subparsers.add_parser("live-readiness", help="read-only controlled execution chain status")
    live_readiness.add_argument("--data-root", type=Path, default=DATA_PATH)
    live_readiness.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    live_readiness.add_argument("--json", action="store_true")
    authorize = subparsers.add_parser("live-authorize", help="explicitly authorize one exact prepared order; never enables execution")
    authorize.add_argument("--approval-id", required=True)
    authorize.add_argument("--approval-sha", required=True)
    authorize.add_argument("--canary-id", required=True)
    authorize.add_argument("--manifest-sha", required=True)
    authorize.add_argument("--amount", required=True)
    authorize.add_argument("--client-order-id", required=True)
    authorize.add_argument("--month", required=True)
    authorize.add_argument("--confirm", required=True)
    authorize.add_argument("--run-id", required=True)
    authorize.add_argument("--data-root", type=Path, default=DATA_PATH)
    authorization_status = subparsers.add_parser("live-authorization-status", help="read-only status of the latest one-shot authorization")
    authorization_status.add_argument("--authorization-id")
    authorization_status.add_argument("--data-root", type=Path, default=DATA_PATH)
    activate = subparsers.add_parser("live-activate", help="create one short-lived activation capability; never submits")
    activate.add_argument("--authorization-id", required=True)
    activate.add_argument("--authorization-sha", required=True)
    activate.add_argument("--approval-id", required=True)
    activate.add_argument("--approval-sha", required=True)
    activate.add_argument("--client-order-id", required=True)
    activate.add_argument("--amount", required=True)
    activate.add_argument("--month", required=True)
    activate.add_argument("--confirm", required=True)
    activate.add_argument("--run-id", required=True)
    activate.add_argument("--data-root", type=Path, default=DATA_PATH)
    activation_status = subparsers.add_parser("live-activation-status", help="read-only status of the latest one-shot activation")
    activation_status.add_argument("--activation-id")
    activation_status.add_argument("--data-root", type=Path, default=DATA_PATH)
    runtime_status = subparsers.add_parser("runtime-execution-status", help="read-only ephemeral execution capability status")
    runtime_status.add_argument("--activation-id")
    runtime_status.add_argument("--data-root", type=Path, default=DATA_PATH)
    enable = subparsers.add_parser("production-enable-prepare", help="prepare governance-only production enablement; never enables execution")
    enable.add_argument("--evidence-json", type=Path, required=True)
    enable.add_argument("--repository-commit-sha", required=True)
    enable.add_argument("--execution-policy-fingerprint", required=True)
    enable.add_argument("--production-contract-fingerprint", required=True)
    enable.add_argument("--account-identity-fingerprint", required=True)
    enable.add_argument("--account-mode", required=True)
    enable.add_argument("--credential-scope", required=True)
    enable.add_argument("--confirm", required=True)
    enable.add_argument("--run-id", required=True)
    enable.add_argument("--data-root", type=Path, default=DATA_PATH)
    enable_status = subparsers.add_parser("production-enablement-status", help="read-only production enablement status")
    enable_status.add_argument("--enablement-id")
    enable_status.add_argument("--data-root", type=Path, default=DATA_PATH)
    subparsers.add_parser("final-readiness-audit", help="read-only final production readiness audit")
    canary_readiness = subparsers.add_parser("bybit-canary-readiness", help="read-only Bybit Spot BTCUSDT canary readiness audit")
    canary_readiness.add_argument("--data-root", type=Path, default=DATA_PATH)
    canary_readiness.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    guardian = subparsers.add_parser("branch-guardian-classify", help="classify a branch diff without merging")
    guardian.add_argument("--branch", required=True); guardian.add_argument("--head-sha", required=True); guardian.add_argument("--base-sha", required=True)
    guardian.add_argument("--changed-files", type=Path, required=True); guardian.add_argument("--diff", type=Path, required=True); guardian.add_argument("--output", type=Path, required=True)
    guardian.add_argument("--bootstrap", action="store_true")
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
    approve = subparsers.add_parser("canary-approve", help="persist a precise ten-minute manual approval")
    approve.add_argument("--run-id", required=True); approve.add_argument("--canary-id", required=True)
    approve.add_argument("--manifest-sha", required=True); approve.add_argument("--amount", required=True)
    approve.add_argument("--client-order-id", required=True); approve.add_argument("--payload-fingerprint", required=True)
    approve.add_argument("--data-root", type=Path, default=DATA_PATH); approve.add_argument("--json", action="store_true")
    execute = subparsers.add_parser("canary-execute", help="one exact manual invocation of the guarded engine")
    execute.add_argument("--run-id", required=True); execute.add_argument("--canary-id", required=True); execute.add_argument("--approval-id", required=True)
    execute.add_argument("--manifest-sha", required=True); execute.add_argument("--approval-sha", required=True)
    execute.add_argument("--authorization-id"); execute.add_argument("--authorization-sha")
    execute.add_argument("--activation-id"); execute.add_argument("--activation-sha")
    execute.add_argument("--enablement-id"); execute.add_argument("--enablement-sha")
    execute.add_argument("--evidence-id"); execute.add_argument("--evidence-sha")
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
    blockers = subparsers.add_parser("production-blockers", help="read-only active production blockers")
    blockers.add_argument("--json", action="store_true")
    contract = subparsers.add_parser("contract-status", help="read-only implemented Bybit contract assumptions")
    contract.add_argument("--json", action="store_true")
    production_status = subparsers.add_parser("production-status", help="read-only aggregate production state")
    production_status.add_argument("--data-root", type=Path, default=DATA_PATH); production_status.add_argument("--ledger", type=Path, default=LEDGER_PATH); production_status.add_argument("--json", action="store_true")
    collect_status = subparsers.add_parser("collect-production-status", help="persist one immutable production status snapshot")
    collect_status.add_argument("--data-root", type=Path, default=DATA_PATH); collect_status.add_argument("--ledger", type=Path, default=LEDGER_PATH); collect_status.add_argument("--json", action="store_true")
    status_history = subparsers.add_parser("production-status-history", help="read immutable production status history")
    status_history.add_argument("--data-root", type=Path, default=DATA_PATH); status_history.add_argument("--limit", type=int, default=20); status_history.add_argument("--json", action="store_true")
    return parser


def _add_ledger_execution_fields(command: argparse.ArgumentParser) -> None:
    command.add_argument("--executed-at", required=True)
    command.add_argument("--usd", required=True)
    command.add_argument("--reference-price", required=True)
    command.add_argument("--btc-quantity")
    command.add_argument("--source", default="Codex CLI")
    command.add_argument("--note", required=True)
    command.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    command.add_argument("--month", help="derive post-write state for this month")
    command.add_argument("--json", action="store_true")


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


def _ledger_state(args: argparse.Namespace) -> dict[str, object]:
    gateway = LedgerGateway(ledger_path=args.ledger)
    state = gateway.get_portfolio_state(args.month).to_dict()
    validate_artifact("portfolio_state", state)
    return {
        "status": "valid",
        "ledger_event_count": state["ledger_event_count"],
        "active_execution_count": state["active_execution_count"],
        "portfolio_state": state,
    }


def _ledger_write(args: argparse.Namespace, operation: str) -> dict[str, object]:
    gateway = LedgerGateway(ledger_path=args.ledger)
    if operation == "record":
        execution_id, created = gateway.record_execution(
            executed_at_utc=args.executed_at,
            executed_usd=Decimal(args.usd),
            reference_price_usdt=Decimal(args.reference_price),
            btc_quantity=Decimal(args.btc_quantity) if args.btc_quantity is not None else None,
            source=args.source,
            note=args.note,
            execution_id=args.execution_id,
            explicit_distinct_execution=args.distinct_execution_intent,
        )
        state_month = args.month or datetime.fromisoformat(args.executed_at.replace("Z", "+00:00")).astimezone(UTC).strftime("%Y-%m")
    elif operation == "correct":
        execution_id, created = gateway.correct_execution(
            supersedes_execution_id=args.target_execution_id,
            executed_at_utc=args.executed_at,
            executed_usd=Decimal(args.usd),
            reference_price_usdt=Decimal(args.reference_price),
            btc_quantity=Decimal(args.btc_quantity) if args.btc_quantity is not None else None,
            source=args.source,
            note=args.note,
            execution_id=args.execution_id,
        )
        state_month = args.month or datetime.fromisoformat(args.executed_at.replace("Z", "+00:00")).astimezone(UTC).strftime("%Y-%m")
    else:
        execution_id, created = gateway.cancel_execution(
            supersedes_execution_id=args.target_execution_id,
            cancelled_at_utc=args.cancelled_at,
            source=args.source,
            note=args.note,
            execution_id=args.execution_id,
        )
        state_month = args.month or datetime.fromisoformat(args.cancelled_at.replace("Z", "+00:00")).astimezone(UTC).strftime("%Y-%m")
    state = gateway.get_portfolio_state(state_month).to_dict()
    validate_artifact("portfolio_state", state)
    return {
        "status": "created" if created else "idempotent",
        "execution_id": execution_id,
        "created": created,
        "ledger_event_count": state["ledger_event_count"],
        "active_execution_count": state["active_execution_count"],
        "portfolio_state": state,
        "message": "Manual ledger reconciliation only — no order submitted",
    }


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
        "ledger_event_count": len(executions),
        "active_execution_count": len(confirmed_executions(executions)),
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


_READINESS_ARTIFACTS = (
    ("decision", ArtifactType.DECISION, "decisions", "decision_id", "created_at_utc", None),
    ("order_intent", ArtifactType.ORDER_INTENT, "order_intents", "order_intent_id", "created_at_utc", None),
    ("canary_manifest", ArtifactType.CANARY_MANIFEST, "canary_manifests", "canary_id", "prepared_at_utc", CANARY_MANIFEST_TTL),
    ("live_execution_approval", ArtifactType.LIVE_EXECUTION_APPROVAL, "live_execution_approvals", "approval_id", "created_at_utc", LIVE_EXECUTION_APPROVAL_TTL),
    ("real_money_authorization", ArtifactType.REAL_MONEY_AUTHORIZATION, "real_money_authorizations", "authorization_id", "created_at_utc", REAL_MONEY_AUTHORIZATION_TTL),
    ("execution_activation", ArtifactType.EXECUTION_ACTIVATION, "execution_activations", "activation_id", "created_at_utc", EXECUTION_ACTIVATION_TTL),
    ("production_enablement", ArtifactType.PRODUCTION_ENABLEMENT, "production_enablements", "enablement_id", "created_at_utc", PRODUCTION_ENABLEMENT_TTL),
)


def _current_commit() -> str:
    return subprocess.run(("git", "rev-parse", "HEAD"), capture_output=True, text=True, check=True).stdout.strip()


def _readiness_latest(store: ArtifactStore, kind: ArtifactType, directory: str) -> tuple[dict[str, object], str] | None:
    if kind is ArtifactType.PRODUCTION_EVIDENCE:
        return _select_production_evidence(store, current_commit=_current_commit(), now=datetime.now(UTC))
    paths = sorted((store.root / directory).glob("*/*/*/*.json"))
    if not paths:
        return None
    path = paths[-1]
    date = datetime(int(path.parts[-4]), int(path.parts[-3]), int(path.parts[-2]), tzinfo=UTC)
    payload = store.read_production_evidence(path.stem) if kind is ArtifactType.PRODUCTION_EVIDENCE else store.read(kind, run_id=path.stem, artifact_date_utc=date)
    return payload, artifact_sha256(payload)


def _select_production_evidence(store: ArtifactStore, *, current_commit: str, now: datetime) -> tuple[dict[str, object], str] | None:
    """Select evidence by validated metadata, never by filename ordering."""
    root = store.root / "production_evidence"
    candidates: list[tuple[dict[str, object], str, datetime, bool, bool]] = []
    for path in root.glob("*/*/*/*.json"):
        try:
            payload = store.read_production_evidence(path.stem)
            created = datetime.fromisoformat(str(payload["created_at_utc"]).replace("Z", "+00:00")).astimezone(UTC)
            expires = datetime.fromisoformat(str(payload["expires_at_utc"]).replace("Z", "+00:00")).astimezone(UTC)
            if created.tzinfo is None or expires.tzinfo is None:
                continue
            digest = artifact_sha256(payload)
            current = payload.get("repository_commit") == current_commit
            unexpired = expires > now
            ready = payload.get("status") == "EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION"
            candidates.append((payload, digest, created, current, unexpired and ready))
        except Exception:
            # Corrupt, schema-invalid, or malformed evidence is never a
            # selection candidate and must not shadow a valid bundle.
            continue
    if not candidates:
        return None

    def newest(rows: list[tuple[dict[str, object], str, datetime, bool, bool]]) -> tuple[dict[str, object], str] | None:
        if not rows:
            return None
        payload, digest, _, _, _ = max(rows, key=lambda row: (row[2], str(row[0].get("evidence_id", ""))))
        return payload, digest

    current_rows = [row for row in candidates if row[3]]
    selected = newest([row for row in current_rows if row[4]])
    if selected is not None:
        return selected
    selected = newest([row for row in current_rows if row[2] and row[0].get("expires_at_utc") and datetime.fromisoformat(str(row[0]["expires_at_utc"]).replace("Z", "+00:00")).astimezone(UTC) > now])
    if selected is not None:
        return selected
    # Keep an expired current-HEAD candidate visible so the report can say
    # EXPIRED. If there is no current-HEAD evidence, expose the newest foreign
    # candidate only so the report can say STALE_REPOSITORY; it is never valid.
    return newest(current_rows) or newest(candidates)


def _assess_readiness_artifact(label: str, payload: Mapping[str, object], digest: str, *, current_commit: str, now: datetime, identity_key: str, created_key: str, ttl: timedelta | None) -> dict[str, object]:
    result: dict[str, object] = {"status": "VALID", "id": payload.get(identity_key), "sha256": digest}
    if payload.get("repository_commit_sha") and payload.get("repository_commit_sha") != current_commit:
        result.update(status="STALE_REPOSITORY", reason="artifact was created for a different repository commit", expected_repository_commit=current_commit, artifact_repository_commit=payload.get("repository_commit_sha"))
        return result
    if ttl is None:
        return result
    try:
        created = datetime.fromisoformat(str(payload[created_key]).replace("Z", "+00:00")).astimezone(UTC)
        expires = datetime.fromisoformat(str(payload["expires_at_utc"]).replace("Z", "+00:00")).astimezone(UTC)
    except (KeyError, TypeError, ValueError) as exc:
        result.update(status="INVALID", reason=f"invalid artifact timestamps: {exc}")
        return result
    if expires <= now:
        result.update(status="EXPIRED", reason="artifact expiry has passed", expires_at_utc=payload.get("expires_at_utc"))
    elif ttl is not None and expires - created > ttl:
        result.update(status="INVALID", reason="artifact TTL exceeds canonical policy")
    elif label in {"real_money_authorization", "execution_activation"} and payload.get("consumed") is True:
        result["status"] = "CONSUMED"
    elif label == "production_enablement" and payload.get("status") == "REVOKED":
        result["status"] = "REVOKED"
    return result


def _live_readiness(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    """Validate one coherent persisted ceremony without collecting or mutating state."""
    config = load_execution_config()
    store = ArtifactStore(args.data_root)
    chain: dict[str, object] = {}
    blockers: list[str] = []
    selected: dict[str, tuple[dict[str, object], str]] = {}
    current_commit = _current_commit()
    now = datetime.now(UTC)
    for label, kind, directory, identity_key, created_key, ttl in _READINESS_ARTIFACTS:
        try:
            found = _readiness_latest(store, kind, directory)
            if found is None:
                chain[label] = {"status": "MISSING", "source": directory}
                blockers.append(f"{label}: MISSING")
                continue
            payload, digest = found
            selected[label] = (payload, digest)
            chain[label] = _assess_readiness_artifact(label, payload, digest, current_commit=current_commit, now=now, identity_key=identity_key, created_key=created_key, ttl=ttl)
            if chain[label]["status"] != "VALID":
                blockers.append(f"{label}: {chain[label]['status']}")
        except Exception as exc:
            chain[label] = {"status": "INVALID", "source": directory, "reason": str(exc)}
            blockers.append(f"{label}: INVALID")

    if all(label in selected for label in ("decision", "order_intent", "canary_manifest", "live_execution_approval", "real_money_authorization", "execution_activation")):
        decision, _ = selected["decision"]; intent, intent_sha = selected["order_intent"]; manifest, manifest_sha = selected["canary_manifest"]
        approval, approval_sha = selected["live_execution_approval"]; authorization, authorization_sha = selected["real_money_authorization"]; activation, _ = selected["execution_activation"]
        identity_pairs = (
            (decision.get("decision_id"), intent.get("decision_id")),
            (intent.get("order_intent_id"), manifest.get("order_intent_id")),
            (manifest.get("canary_id"), approval.get("canary_manifest_id")),
            (approval.get("approval_id"), authorization.get("approval_id")),
            (authorization.get("authorization_id"), activation.get("authorization_id")),
            (intent.get("client_order_id"), approval.get("client_order_id"), authorization.get("client_order_id"), activation.get("client_order_id")),
            (str(intent.get("quote_amount_usd")), str(approval.get("quote_amount_usdt")), str(authorization.get("exact_quote_amount_usdt")), str(activation.get("exact_quote_amount_usdt"))),
            (str(intent.get("created_at_utc", ""))[:7], approval.get("calendar_month"), authorization.get("calendar_month"), activation.get("calendar_month")),
            (approval.get("one_shot_identity"), authorization.get("one_shot_identity"), activation.get("one_shot_identity")),
        )
        sha_pairs = (
            (approval.get("canary_manifest_sha256"), manifest_sha),
            (approval.get("order_intent_sha256"), intent_sha),
            (authorization.get("approval_sha256"), approval_sha),
            (activation.get("approval_sha256"), approval_sha),
            (activation.get("authorization_sha256"), authorization_sha),
        )
        if any(len(set(map(str, pair))) != 1 for pair in identity_pairs + sha_pairs):
            blockers.insert(0, "ceremony artifacts do not share one exact chain: BINDING_MISMATCH")
            for item in chain.values():
                if item.get("status") == "VALID":
                    item.update(status="BINDING_MISMATCH", reason="selected artifacts do not share exact identity/SHA bindings")
        for payload in (approval, authorization, activation):
            if payload.get("repository_commit_sha") != current_commit:
                if "ceremony artifacts are stale for current repository commit" not in blockers:
                    blockers.insert(0, "ceremony artifacts are stale for current repository commit")

    evidence_result: dict[str, object] = {"status": "MISSING"}
    evidence_found = _readiness_latest(store, ArtifactType.PRODUCTION_EVIDENCE, "production_evidence")
    if evidence_found:
        evidence, evidence_sha = evidence_found
        evidence_status = "VALID" if evidence.get("repository_commit") == current_commit and evidence.get("status") == "EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION" else ("STALE_REPOSITORY" if evidence.get("repository_commit") != current_commit else "NOT_READY")
        try:
            if datetime.fromisoformat(str(evidence["expires_at_utc"]).replace("Z", "+00:00")).astimezone(UTC) <= now:
                evidence_status = "EXPIRED"
        except (KeyError, TypeError, ValueError):
            evidence_status = "INVALID"
        evidence_result = {"status": evidence_status, "id": evidence.get("evidence_id"), "sha256": evidence_sha, "repository_commit": evidence.get("repository_commit")}
    if evidence_result["status"] != "VALID":
        blockers.append(f"production evidence: {evidence_result['status']}")
    stale = any(item.get("status") == "STALE_REPOSITORY" for item in chain.values()) or evidence_result["status"] == "STALE_REPOSITORY"
    if stale and "ceremony artifacts are stale for current repository commit" not in blockers:
        blockers.insert(0, "ceremony artifacts are stale for current repository commit")
    safety = {"live_execution_enabled": config.live_execution_enabled, "kill_switch": config.kill_switch, "order_submission": config.order_submission, "monthly_cap_usd": str(config.monthly_cap_usd)}
    if config.live_execution_enabled or not config.kill_switch or config.order_submission != "not_implemented":
        blockers.append("checked-in execution policy is not fail-safe")

    blockers = list(dict.fromkeys(blockers))

    if blockers:
        if stale or evidence_result["status"] in {"MISSING", "EXPIRED", "NOT_READY", "INVALID"}:
            next_command = "btc-dca collect-production-evidence --data-root data --ledger ledger/executions.jsonl"
        elif chain.get("production_enablement", {}).get("status") != "VALID":
            next_command = "btc-dca production-enable-prepare"
        else:
            next_command = None

        return ({
            "status": "BLOCKED",
            "final_state": "BLOCKED",
            "repository_commit": current_commit,
            "chain": chain,
            "production_evidence": evidence_result,
            "safety": safety,
            "blockers": blockers,
            "next_allowed_command": next_command,
            "message": "NO ORDER EXECUTED",
        }, EXIT_BLOCKED)

    return ({
        "status": "READY_FOR_ONE_SHOT_EXECUTION",
        "final_state": "READY_FOR_ONE_SHOT_EXECUTION",
        "repository_commit": current_commit,
        "chain": chain,
        "production_evidence": evidence_result,
        "safety": safety,
        "blockers": [],
        "next_allowed_command": "btc-dca canary-execute",
        "message": "NO ORDER EXECUTED",
    }, 0)


def _live_authorize(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    store = ArtifactStore(args.data_root)
    authorization, receipt = authorize_persisted_approval(
        store=store, approval_id=args.approval_id, approval_sha256=args.approval_sha,
        canary_manifest_id=args.canary_id, canary_manifest_sha256=args.manifest_sha,
        amount=args.amount, client_order_id=args.client_order_id,
        calendar_month=args.month, confirmation=args.confirm,
        run_id=args.run_id, now_utc=datetime.now(UTC),
    )
    config = load_execution_config()
    return ({
        "status": "AUTHORIZED",
        "authorization_id": authorization.authorization_id,
        "authorization_sha256": receipt.sha256,
        "one_shot_identity": authorization.one_shot_identity,
        "runtime": {
            "live_execution_enabled": config.live_execution_enabled,
            "kill_switch": config.kill_switch,
            "order_submission": config.order_submission,
            "authorization": "NOT_AUTHORIZED",
            "execution_status": "BLOCKED",
        },
        "message": "NO ORDER EXECUTED",
    }, 0)


def _live_authorization_status(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    return read_authorization_status(store=ArtifactStore(args.data_root), authorization_id=args.authorization_id), 0


def _live_activate(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    activation, receipt = create_execution_activation(
        store=ArtifactStore(args.data_root), authorization_id=args.authorization_id,
        authorization_sha256=args.authorization_sha, approval_id=args.approval_id,
        approval_sha256=args.approval_sha, client_order_id=args.client_order_id,
        amount=args.amount, calendar_month=args.month, phrase=args.confirm,
        run_id=args.run_id, now_utc=datetime.now(UTC),
    )
    return {
        "status": "ACTIVE", "activation_id": activation.activation_id,
        "activation_sha256": receipt.sha256, "expires_at_utc": activation.payload["expires_at_utc"],
        "one_shot_identity": activation.one_shot_identity,
        "execution": "BLOCKED", "message": "NO ORDER EXECUTED",
    }, 0


def _live_activation_status(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    return read_activation_status(store=ArtifactStore(args.data_root), activation_id=args.activation_id), 0


def _runtime_execution_status(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    """Report runtime capability state without creating or enabling anything."""
    config = load_execution_config()
    activation = read_activation_status(store=ArtifactStore(args.data_root), activation_id=args.activation_id)
    activation_state = str(activation.get("activation", "NOT_ACTIVATED"))
    capability_state = {
        "EXPIRED": "EXPIRED",
        "CONSUMED": "CONSUMED",
        "REVOKED": "ABSENT",
    }.get(activation_state, "ABSENT")
    return {
        "canonical_policy": "BLOCKED",
        "ephemeral_capability": capability_state,
        "execution": "BLOCKED",
        "live_execution_enabled": config.live_execution_enabled,
        "kill_switch": config.kill_switch,
        "order_submission": config.order_submission,
        "message": "NO ORDER EXECUTED",
    }, 0


def _production_enable_prepare(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    enablement, receipt = prepare_production_enablement(
        store=ArtifactStore(args.data_root), evidence=read_json_object(args.evidence_json),
        repository_commit_sha=args.repository_commit_sha,
        execution_policy_fingerprint=args.execution_policy_fingerprint,
        production_contract_fingerprint=args.production_contract_fingerprint,
        account_identity_fingerprint=args.account_identity_fingerprint,
        account_mode=args.account_mode, credential_scope=args.credential_scope,
        confirmation=args.confirm, run_id=args.run_id, now_utc=datetime.now(UTC),
    )
    return {"enablement": "PREPARED", "enablement_id": enablement.enablement_id,
            "enablement_sha256": receipt.sha256, "execution": "BLOCKED",
            "message": "NO ORDER EXECUTED"}, EXIT_BLOCKED


def _production_enable_status(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    return read_enablement_status(store=ArtifactStore(args.data_root), enablement_id=args.enablement_id), 0


def _final_readiness_audit() -> tuple[dict[str, object], int]:
    result = final_readiness_audit()
    return result, {"CORRUPT": EXIT_CORRUPT, "RECONCILIATION_REQUIRED": EXIT_RECONCILIATION}.get(result["readiness"], EXIT_BLOCKED)


def _bybit_canary_readiness(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    result = bybit_canary_readiness(data_root=args.data_root, ledger_path=args.ledger)
    return result, 0 if result["readiness"] == "CANARY_READY_READ_ONLY" else EXIT_BLOCKED


def _branch_guardian_classify(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    result = classify_branch(branch=args.branch, head_sha=args.head_sha, base_sha=args.base_sha,
        changed_files=args.changed_files.read_text(encoding="utf-8").splitlines(), diff_text=args.diff.read_text(encoding="utf-8"), bootstrap=args.bootstrap)
    validate_artifact("branch_guardian_result", result)
    write_result(result, args.output)
    return result, 0 if result["classification"] != "FAIL" else EXIT_CORRUPT


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


def _approval_from_dict(payload: dict[str, object]) -> LiveApproval | LiveExecutionApproval:
    if str(payload.get("schema_version")) == "5.4.0B":
        return LiveExecutionApproval(payload)
    return LiveApproval(str(payload["decision_id"]), str(payload["order_intent_id"]), str(payload["client_order_id"]), Decimal(str(payload["approved_amount_usdt"])), str(payload["approved_at_utc"]), str(payload["expires_at_utc"]), str(payload["approval_id"]), str(payload["canary_id"]), str(payload["manifest_sha256"]), str(payload["order_payload_fingerprint"]), str(payload["exchange"]), str(payload["market_type"]), str(payload["symbol"]), str(payload["side"]), str(payload["order_type"]), bool(payload["standing_authorization"]))


def _intent_from_dict(payload: dict[str, object]) -> OrderIntent:
    return OrderIntent(str(payload["schema_version"]), str(payload["strategy_id"]), str(payload["strategy_version"]), str(payload["run_id"]), str(payload["decision_id"]), str(payload["order_intent_id"]), str(payload["created_at_utc"]), str(payload["exchange"]), str(payload["market_type"]), str(payload["symbol"]), str(payload["side"]), Decimal(str(payload["quote_amount_usd"])), str(payload["expected_mode"]), str(payload["client_order_id"]), Decimal(str(payload["monthly_spent_before_usd"])), Decimal(str(payload["remaining_budget_before_usd"])), str(payload["safety_validation_status"]), bool(payload["live_execution_requested"]), bool(payload["live_execution_enabled"]), bool(payload.get("no_order_executed", True)))


def _latest_verified_production_evidence(store: ArtifactStore) -> tuple[dict[str, object], str]:
    service = ProductionEvidenceService(data_root=store.root, ledger_path=LEDGER_PATH)
    evidence = service.latest()
    if evidence is None:
        raise ValueError("verified production evidence is required before approval")
    repository = ProductionReadinessService._repo_status()
    if repository.get("dirty"):
        raise ValueError("repository must be clean before approval")
    if evidence.get("repository_commit") != repository.get("commit"):
        raise ValueError("production evidence repository commit does not match the current repository")
    verification = service.verify(
        str(evidence.get("evidence_id")),
        current_commit=str(repository.get("commit")),
        current_account_fingerprint=str(evidence.get("account_identity_fingerprint")),
    )
    if verification.get("status") != "VALID_READY_FOR_SEPARATE_AUTHORIZATION":
        raise ValueError(f"production evidence is not currently valid and ready: {verification.get('reason', verification.get('status'))}")
    checks = {str(item.get("check_id")): item for item in evidence.get("check_results", []) if isinstance(item, dict)}
    required = ("BYBIT_ACCOUNT", "BYBIT_CREDENTIAL_SCOPE", "BYBIT_LIABILITIES", "BYBIT_SPOT_AVAILABLE_BALANCE", "BYBIT_INSTRUMENT", "CLOCK_SKEW", "UNRESOLVED_OPERATIONS")
    if any(checks.get(check_id, {}).get("status") != "PASS" for check_id in required):
        raise ValueError("required production evidence checks are incomplete")
    return evidence, artifact_sha256(evidence)


def _canary_approve(args: argparse.Namespace) -> dict[str, object]:
    store = ArtifactStore(args.data_root)
    manifest, digest = store.find_artifact(ArtifactType.CANARY_MANIFEST, identity_field="canary_id", identity_value=args.canary_id)
    if digest != args.manifest_sha or manifest.get("canary_status") != "READY_FOR_MANUAL_APPROVAL" or manifest["client_order_id"] != args.client_order_id or manifest["approved_amount_usdt"] != args.amount or manifest["order_payload_fingerprint"] != args.payload_fingerprint:
        raise ValueError("approval inputs do not bind exactly to the persisted manifest")
    decision, decision_sha = store.find_artifact(ArtifactType.DECISION, identity_field="decision_id", identity_value=str(manifest["decision_id"]))
    intent_payload, intent_sha = store.find_artifact(ArtifactType.ORDER_INTENT, identity_field="order_intent_id", identity_value=str(manifest["order_intent_id"]))
    if any((decision.get("decision_id") != manifest.get("decision_id"), intent_payload.get("decision_id") != manifest.get("decision_id"), intent_payload.get("client_order_id") != manifest.get("client_order_id"))):
        raise ValueError("manifest, Decision, and OrderIntent identity binding is invalid")
    evidence, _ = _latest_verified_production_evidence(store)
    now = datetime.now(UTC)
    try:
        manifest_expires = datetime.fromisoformat(str(manifest["expires_at_utc"]).replace("Z", "+00:00")).astimezone(UTC)
    except (KeyError, ValueError) as exc:
        raise ValueError("CanaryManifest expiry is malformed") from exc
    if now >= manifest_expires or manifest_expires - now < timedelta(minutes=10):
        raise ValueError("CanaryManifest does not have a full ten-minute approval window remaining")
    required_manifest = ("monthly_spent_usd", "remaining_budget_usd", "account_mode", "credential_classification", "authoritative_available_usdt", "instrument_min_order_amt", "instrument_qty_step", "instrument_tick_size", "pre_submission_state", "liability_detected")
    if any(manifest.get(key) is None for key in required_manifest):
        raise ValueError("CanaryManifest lacks required persisted readiness evidence")
    if manifest["liability_detected"] is not False or manifest["pre_submission_state"] != "conclusively_absent":
        raise ValueError("CanaryManifest readiness evidence is not safe for approval")
    evidence_check = {str(item.get("check_id")): item for item in evidence.get("check_results", []) if isinstance(item, dict)}
    approval = LiveExecutionApproval.prepare(
        now_utc=now,
        repository_commit_sha=str(evidence["repository_commit"]),
        strategy_id=str(manifest["strategy_id"]), strategy_version=str(manifest["strategy_version"]),
        canary_manifest_id=str(manifest["canary_id"]), canary_manifest_sha256=digest,
        decision_id=str(manifest["decision_id"]), decision_sha256=decision_sha,
        order_intent_id=str(manifest["order_intent_id"]), order_intent_sha256=intent_sha,
        client_order_id=str(manifest["client_order_id"]), quote_amount_usdt=manifest["approved_amount_usdt"],
        calendar_month=str(manifest["calendar_month"]), monthly_spent_usdt=manifest["monthly_spent_usd"],
        remaining_monthly_budget_usdt=manifest["remaining_budget_usd"],
        account_identity_fingerprint=str(evidence["account_identity_fingerprint"]),
        account_mode=str(manifest["account_mode"]), credential_scope_classification="SPOT_TRADE_NO_WITHDRAW",
        liability_state="NONE",
        spot_usdt_availability_fingerprint=canonical_fingerprint({"authoritative_available_usdt": manifest["authoritative_available_usdt"], "availability_method": manifest["availability_method"]}),
        instrument_metadata_fingerprint=canonical_fingerprint({key: manifest[key] for key in ("instrument_min_order_amt", "instrument_qty_step", "instrument_tick_size")} ),
        clock_skew_evidence_fingerprint=canonical_fingerprint(evidence_check["CLOCK_SKEW"]),
        reconciliation_evidence_fingerprint=canonical_fingerprint({"client_order_id": manifest["client_order_id"], "state": manifest["pre_submission_state"]}),
        production_contract_fingerprint=production_contract_fingerprint(),
        execution_safety_config_fingerprint=execution_safety_config_fingerprint(load_execution_config()),
        manifest_expires_at_utc=str(manifest["expires_at_utc"]), state="PREPARED", ttl=timedelta(minutes=10),
    )
    receipt = approval.persist(store, run_id=args.run_id)
    # Keep the older preparation artifact for legacy operations/recovery
    # readers; the returned approval and all Phase 5.4C+ consumers use the
    # canonical LiveExecutionApproval above.
    legacy_approval = LiveApproval.for_manifest(manifest, digest, now_utc=now)
    store.persist(ArtifactType.LIVE_APPROVAL, legacy_approval, run_id=args.run_id)
    OperationsService(data_root=args.data_root, ledger_path=LEDGER_PATH).record_event(action="approval_created", result="persisted", reason="exact manifest-bound manual approval", run_id=args.run_id, artifact_ids={"canary_id": approval.payload["canary_manifest_id"], "approval_id": approval.approval_id, "client_order_id": approval.payload["client_order_id"]})
    return {"status": "approval_persisted", "approval_id": approval.approval_id, "approval_sha256": receipt.sha256, "summary": {"exchange": "Bybit", "market": "Spot", "symbol": "BTCUSDT", "side": "Buy", "order_type": "Market", "quote_amount_usdt": str(approval.payload["quote_amount_usdt"]), "leverage": 0, "canary_id": approval.payload["canary_manifest_id"], "client_order_id": approval.payload["client_order_id"], "manifest_sha256": approval.payload["canary_manifest_sha256"], "expires_at_utc": approval.expires_at_utc}}


def _load_exact_execution_artifacts(args: argparse.Namespace) -> tuple[ArtifactStore, dict[str, object], dict[str, object], LiveApproval | LiveExecutionApproval, str, str, OrderIntent, dict[str, object]]:
    store = ArtifactStore(args.data_root)
    manifest, manifest_sha = store.find_artifact(ArtifactType.CANARY_MANIFEST, identity_field="canary_id", identity_value=args.canary_id)
    try:
        approval_payload, approval_sha = store.find_artifact(ArtifactType.LIVE_EXECUTION_APPROVAL, identity_field="approval_id", identity_value=args.approval_id)
    except ArtifactNotFoundError:
        # Existing legacy recovery artifacts remain readable; new operator
        # approvals always use LIVE_EXECUTION_APPROVAL above.
        approval_payload, approval_sha = store.find_artifact(ArtifactType.LIVE_APPROVAL, identity_field="approval_id", identity_value=args.approval_id)
    if manifest_sha != args.manifest_sha or approval_sha != args.approval_sha:
        raise ValueError("supplied immutable digest does not match persisted artifact")
    approval = _approval_from_dict(approval_payload)
    intent_payload, _ = store.find_artifact(ArtifactType.ORDER_INTENT, identity_field="order_intent_id", identity_value=str(manifest["order_intent_id"]))
    decision, _ = store.find_artifact(ArtifactType.DECISION, identity_field="decision_id", identity_value=str(manifest["decision_id"]))
    return store, manifest, approval_payload, approval, manifest_sha, approval_sha, _intent_from_dict(intent_payload), decision


def _latest_artifact(store: ArtifactStore, kind: ArtifactType) -> tuple[dict[str, object], str]:
    directories = {
        ArtifactType.PRODUCTION_ENABLEMENT: "production_enablements",
        ArtifactType.PRODUCTION_EVIDENCE: "production_evidence",
    }
    root = store.root / directories[kind]
    paths = sorted(root.glob("*/*/*/*.json"))
    if not paths:
        raise ArtifactNotFoundError(f"{kind.value} is not persisted")
    path = paths[-1]
    date = datetime(int(path.parts[-4]), int(path.parts[-3]), int(path.parts[-2]), tzinfo=UTC)
    payload = store.read_production_evidence(path.stem) if kind is ArtifactType.PRODUCTION_EVIDENCE else store.read(kind, run_id=path.stem, artifact_date_utc=date)
    return payload, artifact_sha256(payload)


def _load_controlled_execution_artifacts(args: argparse.Namespace) -> dict[str, object]:
    """Load the complete Phase 5.4B-E chain from verified immutable bytes."""
    store = ArtifactStore(args.data_root)
    approval, approval_sha = store.find_artifact(ArtifactType.LIVE_EXECUTION_APPROVAL, identity_field="approval_id", identity_value=args.approval_id)
    manifest, manifest_sha = store.find_artifact(ArtifactType.CANARY_MANIFEST, identity_field="canary_id", identity_value=args.canary_id)
    decision, decision_sha = store.find_artifact(ArtifactType.DECISION, identity_field="decision_id", identity_value=str(approval["decision_id"]))
    intent, intent_sha = store.find_artifact(ArtifactType.ORDER_INTENT, identity_field="order_intent_id", identity_value=str(approval["order_intent_id"]))
    authorization_id = getattr(args, "authorization_id", None)
    authorization, authorization_sha = store.find_artifact(
        ArtifactType.REAL_MONEY_AUTHORIZATION,
        identity_field="authorization_id" if authorization_id else "one_shot_identity",
        identity_value=authorization_id or str(approval["one_shot_identity"]),
    )
    activation_id = getattr(args, "activation_id", None)
    activation, activation_sha = store.find_artifact(
        ArtifactType.EXECUTION_ACTIVATION,
        identity_field="activation_id" if activation_id else "one_shot_identity",
        identity_value=activation_id or str(approval["one_shot_identity"]),
    )
    enablement_id = getattr(args, "enablement_id", None)
    enablement, enablement_sha = (
        store.find_artifact(ArtifactType.PRODUCTION_ENABLEMENT, identity_field="enablement_id", identity_value=enablement_id)
        if enablement_id else _latest_artifact(store, ArtifactType.PRODUCTION_ENABLEMENT)
    )
    evidence_id = getattr(args, "evidence_id", None)
    evidence, evidence_sha = (
        (store.read_production_evidence(evidence_id), artifact_sha256(store.read_production_evidence(evidence_id)))
        if evidence_id else _latest_artifact(store, ArtifactType.PRODUCTION_EVIDENCE)
    )
    supplied = {
        "manifest_sha": getattr(args, "manifest_sha", None),
        "approval_sha": getattr(args, "approval_sha", None),
        "authorization_sha": getattr(args, "authorization_sha", None),
        "activation_sha": getattr(args, "activation_sha", None),
        "enablement_sha": getattr(args, "enablement_sha", None),
        "evidence_sha": getattr(args, "evidence_sha", None),
    }
    actual = {"manifest_sha": manifest_sha, "approval_sha": approval_sha, "authorization_sha": authorization_sha, "activation_sha": activation_sha, "enablement_sha": enablement_sha, "evidence_sha": evidence_sha}
    if any(value is not None and value != actual[key] for key, value in supplied.items()):
        raise ValueError("supplied immutable artifact SHA does not match persisted bytes")
    if manifest.get("canary_id") != args.canary_id or approval.get("approval_id") != args.approval_id:
        raise ValueError("canary or approval identity does not match persisted artifacts")
    if manifest.get("decision_id") != approval.get("decision_id") or manifest.get("order_intent_id") != approval.get("order_intent_id"):
        raise ValueError("manifest and approval identity binding is invalid")
    return {
        "store": store, "manifest": manifest, "manifest_sha": manifest_sha,
        "approval": approval, "approval_sha": approval_sha,
        "authorization": authorization, "authorization_sha": authorization_sha,
        "activation": activation, "activation_sha": activation_sha,
        "enablement": enablement, "enablement_sha": enablement_sha,
        "evidence": evidence, "evidence_sha": evidence_sha,
        "decision": decision, "decision_sha": decision_sha,
        "intent": _intent_from_dict(intent), "intent_sha": intent_sha,
    }


def _fresh_gate_snapshot(*, client, intent: OrderIntent, approval: dict[str, object], authorization: dict[str, object], approval_sha256: str, authorization_sha256: str, config, ledger_path: Path, now_utc: datetime) -> dict[str, object]:
    """Collect one independent Gate A/B snapshot from live read-only sources."""
    credential = client.credential_info()
    account = client.account_info()
    balances = {row.coin: row for row in client.wallet_balances()}
    availability = client.spot_quote_availability()
    available_usdt = validate_spot_quote_availability(availability, now=now_utc, policy=PRODUCTION_AVAILABILITY_POLICY)
    instrument = client.instrument_rules()
    instrument.validate_quote(intent.quote_amount_usdt)
    server_ms = client.server_time_ms()
    local_ms = int(now_utc.timestamp() * 1000)
    clock_delta_ms = server_ms - local_ms
    submission_state = client.submission_state(intent.client_order_id)
    if submission_state != "conclusively_absent":
        raise ValueError("fresh reconciliation is not conclusively absent")
    usdt, btc = balances.get("USDT"), balances.get("BTC")
    if usdt is None or btc is None:
        raise ValueError("fresh BTC/USDT wallet evidence is incomplete")
    if usdt.has_liability or btc.has_liability:
        raise ValueError("fresh wallet liability detected")
    permissions = {str(key): tuple(str(value) for value in values) for key, values in credential.permissions.items()}
    spot_actions = set(permissions.get("Spot", ()))
    credential_scope_accepted = (
        credential.classification.value == "TRADE_CAPABLE"
        and not (set(permissions) - {"Spot", "Wallet"})
        and bool(spot_actions & {"SpotTrade", "OrderEntry", "SpotOrder"})
        and not any(action.lower() in {"withdrawal", "withdraw"} for action in permissions.get("Wallet", ()))
    )
    if not credential_scope_accepted:
        raise ValueError("fresh credential scope is not accepted")
    if abs(clock_delta_ms) > 5000:
        raise ValueError("fresh Bybit clock evidence exceeds the five-second drift bound")
    if account.unified_margin_status not in {5, 6, "5", "6"} or account.margin_mode not in {"REGULAR_MARGIN", "ISOLATED_MARGIN", "PORTFOLIO_MARGIN"} or account.spot_hedging_status != "OFF":
        raise ValueError("fresh account mode is not compatible with Spot BTCUSDT")
    expected_mode = str(approval.get("account_mode"))
    actual_mode = expected_mode if expected_mode in {"spot", "UTA"} else str(account.margin_mode)
    if credential.identity is None:
        raise ValueError("fresh account identity is unavailable")
    account_identity = canonical_fingerprint({
        "exchange": "Bybit", "identity": credential.identity,
        "unified_margin_status": str(account.unified_margin_status),
        "margin_mode": str(account.margin_mode),
        "spot_hedging_status": str(account.spot_hedging_status),
    })
    if account_identity != str(approval.get("account_identity_fingerprint")):
        raise ValueError("fresh account identity differs from the approved identity")
    monthly_spent = sum((execution.executed_usd for execution in confirmed_executions(read_executions(ledger_path)) if execution.executed_at_utc[:7] == str(approval["calendar_month"])), Decimal("0"))
    remaining = Decimal("500") - monthly_spent
    request = {**ORDER_CONTRACT, "qty": str(intent.quote_amount_usdt), "orderLinkId": intent.client_order_id}
    return {
        "safety_config": config,
        "real_money_authorization": authorization,
        "approval_sha256": approval_sha256,
        "authorization_sha256": authorization_sha256,
        "repository_commit_sha": approval.get("repository_commit_sha"),
        "calendar_month": approval.get("calendar_month"),
        "monthly_spent_usdt": str(monthly_spent),
        "remaining_monthly_budget_usdt": str(remaining),
        "account_identity_fingerprint": account_identity,
        "account_mode": actual_mode,
        "credential_scope_classification": "SPOT_TRADE_NO_WITHDRAW",
        "liability_state": "NONE",
        # Availability contains Decimal values; fingerprint an explicit,
        # JSON-stable read-only projection rather than the dataclass repr.
        "spot_usdt_availability_fingerprint": canonical_fingerprint({
            "amount_usdt": str(availability.amount_usdt),
            "source_endpoint": availability.source_endpoint,
            "source_field": availability.source_field,
            "account_type": availability.account_type,
            "authoritative": availability.authoritative,
            "observed_at_utc": availability.observed_at_utc,
        }),
        "instrument_metadata_fingerprint": canonical_fingerprint({key: str(value) for key, value in instrument.__dict__.items()}),
        "clock_skew_evidence_fingerprint": canonical_fingerprint({"server_ms": server_ms, "local_ms": local_ms, "delta_ms": clock_delta_ms}),
        "reconciliation_evidence_fingerprint": canonical_fingerprint({"client_order_id": intent.client_order_id, "state": submission_state}),
        "production_contract_fingerprint": str(approval.get("production_contract_fingerprint")),
        "execution_safety_config_fingerprint": str(approval.get("execution_safety_config_fingerprint")),
        "ledger_valid": True,
        "credential_scope_accepted": True,
        "withdrawal_enabled": False,
        "btc_liability": False,
        "usdt_liability": False,
        "borrow_state": False,
        "spot_usdt_sufficient": available_usdt >= intent.quote_amount_usdt,
        "spot_availability_fresh": True,
        "instrument_metadata_fresh": True,
        "clock_evidence_fresh": True,
        "reconciliation_evidence_fresh": True,
        "duplicate_execution": False,
        "duplicate_attempt": False,
        "reconciliation_required": False,
        "request_fingerprint": canonical_fingerprint(request),
    }


def _gate_d_evidence(bundle: dict[str, object], *, enablement: dict[str, object]) -> dict[str, object]:
    """Project persisted evidence into Gate D's explicit boolean contract.

    Gate D may use the persisted readiness bundle for deployment-level checks;
    dynamic Gate A/B evidence is collected separately and is never projected
    from this bundle.
    """
    projected = project_gate_d_evidence(
        bundle,
        contract_valid=bool(bundle.get("production_contract_fingerprint")),
    )
    projected.update({
        "account_identity_fingerprint": bundle.get("account_identity_fingerprint"),
        "account_mode": enablement.get("expected_account_mode"),
        "credential_scope": enablement.get("allowed_credential_scope"),
        "production_contract_fingerprint": enablement.get("production_contract_fingerprint"),
        "execution_policy_fingerprint": enablement.get("execution_policy_fingerprint"),
    })
    return projected


def _canary_execute(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = load_execution_config()
    try:
        chain = _load_controlled_execution_artifacts(args)
        verifier = ControlledProductionExecutor(engine=None, store=chain["store"])
        verifier._verify_persisted_chain(
            enablement=chain["enablement"], enablement_sha256=chain["enablement_sha"],
            authorization=chain["authorization"], authorization_sha256=chain["authorization_sha"],
            activation=chain["activation"], activation_sha256=chain["activation_sha"],
            approval=chain["approval"], approval_sha256=chain["approval_sha"],
            manifest=chain["manifest"], manifest_sha256=chain["manifest_sha"],
            intent=chain["intent"], decision=chain["decision"],
            decision_sha256=chain["decision_sha"], order_intent_sha256=chain["intent_sha"],
            evidence=chain["evidence"], evidence_sha256=chain["evidence_sha"],
            repository_commit_sha=str(chain["approval"].get("repository_commit_sha", "")),
            calendar_month=args.month,
        )
        policy = derive_ephemeral_execution_policy(
            canonical_config=config,
            enablement=chain["enablement"], enablement_sha256=chain["enablement_sha"],
            authorization=chain["authorization"], authorization_sha256=chain["authorization_sha"],
            activation=chain["activation"], activation_sha256=chain["activation_sha"],
            approval=chain["approval"], approval_sha256=chain["approval_sha"],
            manifest=chain["manifest"], manifest_sha256=chain["manifest_sha"],
            decision=chain["decision"], decision_sha256=chain["decision_sha"],
            intent=chain["intent"], intent_sha256=chain["intent_sha"],
            evidence=chain["evidence"], repository_commit_sha=str(chain["approval"].get("repository_commit_sha", "")),
            calendar_month=args.month, now_utc=datetime.now(UTC),
        )
        runtime_config = policy.runtime_config
        policy.assert_current(
            client_order_id=chain["intent"].client_order_id,
            quote_amount_usdt=chain["intent"].quote_amount_usdt,
            calendar_month=args.month,
            now_utc=datetime.now(UTC),
        )
        preliminary_d = gate_d(
            enablement=chain["enablement"], enablement_sha256=chain["enablement_sha"],
            runtime_config=runtime_config, store=chain["store"],
            repository_commit_sha=str(chain["approval"].get("repository_commit_sha", "")),
            evidence=_gate_d_evidence(chain["evidence"], enablement=chain["enablement"]), now_utc=datetime.now(UTC),
        )
        if preliminary_d.status.value != "READY_FOR_SUBMISSION":
            return ({"status": "blocked", "gate": "D", "reasons": preliminary_d.reasons, "outcome_category": "request_not_sent", "message": "NO ORDER SUBMITTED"}, EXIT_BLOCKED)
        client = _private_read_client_factory()
        reconciler = _post_ack_reconciler_factory(client)
        gate_a_current = _fresh_gate_snapshot(client=client, intent=chain["intent"], approval=chain["approval"], authorization=chain["authorization"], approval_sha256=chain["approval_sha"], authorization_sha256=chain["authorization_sha"], config=runtime_config, ledger_path=args.ledger, now_utc=datetime.now(UTC))
        gate_b_current = _fresh_gate_snapshot(client=client, intent=chain["intent"], approval=chain["approval"], authorization=chain["authorization"], approval_sha256=chain["approval_sha"], authorization_sha256=chain["authorization_sha"], config=runtime_config, ledger_path=args.ledger, now_utc=datetime.now(UTC))

        def lazy_transport():
            return _submission_transport_factory(os.environ.get("BYBIT_API_KEY", ""), os.environ.get("BYBIT_API_SECRET", ""))

        result = ControlledProductionExecutor(engine=LiveOrderEngine(artifact_store=chain["store"]), store=chain["store"]).execute(
            enablement=chain["enablement"], enablement_sha256=chain["enablement_sha"],
            authorization=chain["authorization"], authorization_sha256=chain["authorization_sha"],
            activation=chain["activation"], activation_sha256=chain["activation_sha"],
            capability=None, approval=chain["approval"], approval_sha256=chain["approval_sha"],
            manifest=chain["manifest"], manifest_sha256=chain["manifest_sha"], intent=chain["intent"],
            decision=chain["decision"], runtime_config=runtime_config, evidence=chain["evidence"],
            repository_commit_sha=str(chain["approval"].get("repository_commit_sha", "")),
            calendar_month=args.month, ledger_path=args.ledger, read_client=client,
            reconciler=reconciler, run_id=args.run_id, transport_factory=lazy_transport,
            decision_sha256=chain["decision_sha"], order_intent_sha256=chain["intent_sha"], evidence_sha256=chain["evidence_sha"],
            gate_a_current=gate_a_current, gate_b_current=gate_b_current,
        )
        if isinstance(result, SubmissionResult):
            payload = result.outcome.to_dict()
            return payload, 0 if payload.get("outcome_category") == "confirmed_execution" else EXIT_RECONCILIATION
        return dict(result), EXIT_BLOCKED
    except (ArtifactNotFoundError, ValueError, KeyError) as exc:
        return ({"status": "blocked", "reason": str(exc), "outcome_category": "request_not_sent", "message": "NO ORDER SUBMITTED"}, EXIT_BLOCKED)


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


def _production_blockers() -> tuple[dict[str, object], int]:
    blockers = active_production_blockers()
    return {"state": "PRODUCTION_BLOCKED_EXTERNAL_CONTRACT" if blockers else "NO_ACTIVE_EXTERNAL_BLOCKERS", "blockers": [item.to_dict() for item in blockers], "real_money_authorization": {"granted": False, "source": "none", "required": True, "status": "NOT_AUTHORIZED"}}, 0


def _contract_status() -> tuple[dict[str, object], int]:
    return contract_status(), 0


def _status_service(args: argparse.Namespace) -> ProductionStatusService:
    return _production_status_service_factory(data_root=args.data_root, ledger_path=args.ledger)


def _production_status(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    result = _status_service(args).evaluate().to_dict()
    code = 5 if result["overall_operator_state"] == "CORRUPT" else (3 if result["overall_operator_state"] == "RECONCILIATION_REQUIRED" else (2 if result["overall_operator_state"] == "ACTION_REQUIRED" else 0))
    return result, code


def _collect_production_status(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    result = _status_service(args).collect()
    alerts = result.get("new_alerts", [])
    notification_status = "not_sent"
    delivery_results: list[dict[str, str]] = []
    for alert in alerts:
        try:
            notification = load_notification_config()
            token = os.environ.get(notification.bot_token_env_var, "")
            chat_id = os.environ.get(notification.chat_id_env_var, "")
            if not token or not chat_id:
                delivery_results.append({"alert_id": str(alert["alert_id"]), "status": "unavailable"})
            else:
                delivery = TelegramNotifier(token, chat_id, transport=TelegramTransport(
                    connect_timeout_seconds=notification.http.connect_timeout_seconds,
                    read_timeout_seconds=notification.http.read_timeout_seconds,
                    max_attempts=notification.http.retry_attempts,
                    backoff_seconds=notification.http.backoff_seconds,
                )).send(str(alert["message"]))
                delivery_results.append({"alert_id": str(alert["alert_id"]), "status": "sent", "message_id": str(delivery.message_id), "attempts": str(delivery.attempts)})
        except Exception:
            # The alert is already durable. Do not retry or remove it here;
            # later collections will see it as covered and won't resend it.
            delivery_results.append({"alert_id": str(alert["alert_id"]), "status": "failed"})
    if delivery_results:
        statuses = {item["status"] for item in delivery_results}
        notification_status = next(iter(statuses)) if len(statuses) == 1 else "partial_or_failed"
        result["notification_deliveries"] = delivery_results
    result["notification_status"] = notification_status
    state = result["snapshot"]["overall_operator_state"]
    code = 5 if state == "CORRUPT" else (3 if state == "RECONCILIATION_REQUIRED" else (2 if state == "ACTION_REQUIRED" else 0))
    return result, code


def _production_status_history(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    rows = _production_status_service_factory(data_root=args.data_root).history(limit=args.limit)
    return {"snapshots": rows, "count": len(rows)}, 0


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
        elif args.command == "live-readiness":
            result, exit_code = _live_readiness(args)
        elif args.command == "live-authorize":
            result, exit_code = _live_authorize(args)
        elif args.command == "live-authorization-status":
            result, exit_code = _live_authorization_status(args)
        elif args.command == "live-activate":
            result, exit_code = _live_activate(args)
        elif args.command == "live-activation-status":
            result, exit_code = _live_activation_status(args)
        elif args.command == "runtime-execution-status":
            result, exit_code = _runtime_execution_status(args)
        elif args.command == "production-enable-prepare":
            result, exit_code = _production_enable_prepare(args)
        elif args.command == "production-enablement-status":
            result, exit_code = _production_enable_status(args)
        elif args.command == "final-readiness-audit":
            result, exit_code = _final_readiness_audit()
        elif args.command == "bybit-canary-readiness":
            result, exit_code = _bybit_canary_readiness(args)
        elif args.command == "branch-guardian-classify":
            result, exit_code = _branch_guardian_classify(args)
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
        elif args.command == "production-blockers":
            result, exit_code = _production_blockers()
        elif args.command == "contract-status":
            result, exit_code = _contract_status()
        elif args.command == "production-status":
            result, exit_code = _production_status(args)
        elif args.command == "collect-production-status":
            result, exit_code = _collect_production_status(args)
        elif args.command == "production-status-history":
            result, exit_code = _production_status_history(args)
        elif args.command == "calculate":
            result = _calculate(args)
        elif args.command == "portfolio":
            result = _portfolio(args)
        elif args.command == "ledger-state":
            result = _ledger_state(args)
        elif args.command == "ledger-record":
            result = _ledger_write(args, "record")
        elif args.command == "ledger-correct":
            result = _ledger_write(args, "correct")
        elif args.command == "ledger-cancel":
            result = _ledger_write(args, "cancel")
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
