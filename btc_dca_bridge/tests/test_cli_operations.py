"""CLI-level synthetic operations/recovery coverage; never creates HTTP clients."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType, make_run_id
from btc_dca_bridge.canary import CanaryPreparer
from btc_dca_bridge.cli import main
from btc_dca_bridge.config import ExecutionConfig
from btc_dca_bridge.execution import InstrumentRules, make_order_intent
from btc_dca_bridge.live_order import ConfirmedFill, ReconciliationEvidence
from btc_dca_bridge.market_data.http import HttpResponse
from btc_dca_bridge.operations import EXIT_BLOCKED, EXIT_CORRUPT, EXIT_RECONCILIATION, EXIT_UNAVAILABLE, OperationLock, OperationLockError, OperationsService
from btc_dca_bridge.private_bybit import AccountInfo, ApiCredentialInfo, CredentialClassification, PrivateApiUnavailableError, WalletBalance


class FakeReader:
    def __init__(self):
        self.credential = ApiCredentialInfo(CredentialClassification.TRADE_CAPABLE, False, {"Spot": ("SpotTrade",), "Wallet": ("WalletRead",)})
        self.account = AccountInfo(6, "REGULAR_MARGIN", "OFF", "fresh")
        self.balances = (WalletBalance("USDT", Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("100"), Decimal("100")), WalletBalance("BTC", Decimal("0.1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("6000")))
        self.rules = InstrumentRules(Decimal("10"), Decimal("0.00001"), Decimal("0.00001"), Decimal("0.01"))
    def credential_info(self): return self.credential
    def account_info(self): return self.account
    def wallet_balances(self): return self.balances
    def instrument_rules(self): return self.rules
    def submission_state(self, unused): return "conclusively_absent"


class FakeTransport:
    def __init__(self, response): self.response, self.calls = response, []
    def submit_spot_market_buy(self, request): self.calls.append(request); return self.response


class FakeReconciler:
    def __init__(self, evidence): self.evidence, self.calls = evidence, []
    def reconcile_after_ack(self, link): self.calls.append(link); return self.evidence


class CliOperationsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.data, self.ledger = self.root / "data", self.root / "executions.jsonl"
        self.ledger.write_text("")
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.run_id = make_run_id(self.now, "cliops001")
        self.recovery_run = make_run_id(self.now + timedelta(seconds=1), "cliops002")
        self.decision = {"schema_version": "1.0.0", "decision_id": "decision_cli_ops", "created_at_utc": self.now.isoformat().replace("+00:00", "Z"), "strategy_id": "btc_adaptive_dca_v1", "strategy_version": "1.0.0", "market_snapshot_id": "market_cli_ops", "drawdown_percent": -10, "fear_greed_index": 20, "base_allocation_usd": 25, "sentiment_multiplier": 1, "calculated_allocation_usd": 25, "monthly_spent_before_usd": 0, "remaining_budget_before_usd": 500, "final_purchase_usd": 25, "status": "approved"}
        self.reader = FakeReader()
        disabled = ExecutionConfig("1.0.0", False, True, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "not_implemented")
        store = ArtifactStore(self.data)
        store.persist(ArtifactType.DECISION, self.decision, run_id=self.run_id)
        self.intent = make_order_intent(self.decision, run_id=self.run_id, created_at_utc=self.decision["created_at_utc"])
        store.persist(ArtifactType.ORDER_INTENT, self.intent, run_id=self.run_id)
        self.manifest = CanaryPreparer(artifact_store=store, client=self.reader, execution_config=disabled, now=lambda: self.now).prepare(self.decision, run_id=self.run_id, calendar_month=self.now.strftime("%Y-%m"), ledger_path=self.ledger).manifest.to_dict()
        self.manifest_payload, self.manifest_sha = store.find_artifact(ArtifactType.CANARY_MANIFEST, identity_field="canary_id", identity_value=self.manifest["canary_id"])
        self.enabled = ExecutionConfig("1.0.0", True, False, True, Decimal("500"), "Bybit", "spot", "BTCUSDT", "implemented")

    def invoke(self, arguments):
        output, error = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(error): code = main(arguments)
        return code, json.loads(output.getvalue()) if output.getvalue() else {}, error.getvalue()

    def approve(self):
        code, result, _ = self.invoke(["canary-approve", "--run-id", self.run_id, "--canary-id", self.manifest["canary_id"], "--manifest-sha", self.manifest_sha, "--amount", "25", "--client-order-id", self.manifest["client_order_id"], "--payload-fingerprint", self.manifest["order_payload_fingerprint"], "--data-root", str(self.data)])
        self.assertEqual(code, 0)
        self.approval_id, self.approval_sha = result["approval_id"], result["approval_sha256"]

    def exact_args(self, command, run_id=None):
        return [command, "--run-id", run_id or self.run_id, "--canary-id", self.manifest["canary_id"], "--approval-id", self.approval_id, "--manifest-sha", self.manifest_sha, "--approval-sha", self.approval_sha, "--data-root", str(self.data), "--ledger", str(self.ledger)]

    def test_full_cli_dry_operations_partial_to_final_is_exactly_once(self):
        self.approve()
        with patch("btc_dca_bridge.operations.load_execution_config", return_value=self.enabled):
            code, status, _ = self.invoke(["ops-status", "--run-id", self.run_id, "--data-root", str(self.data), "--ledger", str(self.ledger), "--json"])
        self.assertEqual((code, status["state"]), (0, "READY_FOR_MANUAL_EXECUTION"))
        code, plan, _ = self.invoke(["ops-plan", "--run-id", self.run_id, "--data-root", str(self.data), "--ledger", str(self.ledger), "--json"])
        self.assertEqual(code, EXIT_BLOCKED)  # checked-in defaults remain authoritative outside the synthetic execute call
        self.assertNotIn("canary-execute", plan["allowed_actions"])
        fill_a = ConfirmedFill("exec-cli-a", "order-cli", self.intent.client_order_id, Decimal("0.0001"), Decimal("12.49"), Decimal("124900"), self.now.isoformat().replace("+00:00", "Z"), Decimal("0.01"), "USDT")
        partial = FakeReconciler(ReconciliationEvidence("partial", "order-cli", (fill_a,), self.intent.client_order_id))
        transport = FakeTransport(HttpResponse(200, {}, json.dumps({"retCode": 0, "result": {"orderId": "order-cli", "orderLinkId": self.intent.client_order_id}}).encode()))
        execute = self.exact_args("canary-execute") + ["--month", self.now.strftime("%Y-%m")]
        with patch("btc_dca_bridge.cli.load_execution_config", return_value=self.enabled), patch("btc_dca_bridge.cli._private_read_client_factory", return_value=self.reader), patch("btc_dca_bridge.cli._submission_transport_factory", return_value=transport), patch("btc_dca_bridge.cli._post_ack_reconciler_factory", return_value=partial):
            code, outcome, error = self.invoke(execute)
        self.assertIn("outcome_category", outcome, error)
        self.assertEqual((code, outcome["outcome_category"], len(transport.calls)), (EXIT_RECONCILIATION, "reconciliation_required", 1))
        self.assertEqual(self.ledger.read_text(), "")
        code, status, _ = self.invoke(["ops-status", "--run-id", self.run_id, "--data-root", str(self.data), "--ledger", str(self.ledger), "--json"])
        self.assertEqual((code, status["state"]), (EXIT_RECONCILIATION, "RECONCILIATION_REQUIRED"))
        self.assertEqual(status["operational_exposure"]["known_unresolved_partial_quote_usdt"], "12.49")
        code, plan, _ = self.invoke(["ops-plan", "--run-id", self.run_id, "--data-root", str(self.data), "--ledger", str(self.ledger), "--json"])
        self.assertEqual(code, EXIT_RECONCILIATION); self.assertEqual(plan["allowed_next_action"], "reconcile-existing")
        fill_b = ConfirmedFill("exec-cli-b", "order-cli", self.intent.client_order_id, Decimal("0.0001"), Decimal("12.50"), Decimal("125000"), (self.now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), Decimal("0.01"), "USDT")
        final = FakeReconciler(ReconciliationEvidence("confirmed", "order-cli", (fill_a, fill_b), self.intent.client_order_id))
        recover = self.exact_args("reconcile-existing", self.recovery_run)
        with patch("btc_dca_bridge.cli._private_read_client_factory", return_value=self.reader), patch("btc_dca_bridge.cli._post_ack_reconciler_factory", return_value=final):
            code, outcome, _ = self.invoke(recover)
        self.assertEqual((code, outcome["outcome_category"]), (0, "confirmed_execution"))
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        self.assertEqual((len(rows), rows[0]["executed_usd"], rows[0]["btc_quantity"]), (1, 24.99, 0.0002))
        replay_run = make_run_id(self.now + timedelta(seconds=2), "cliops003")
        with patch("btc_dca_bridge.cli._private_read_client_factory", return_value=self.reader), patch("btc_dca_bridge.cli._post_ack_reconciler_factory", return_value=final): code, _, _ = self.invoke(self.exact_args("reconcile-existing", replay_run))
        self.assertEqual(code, 0); self.assertEqual(len(self.ledger.read_text().splitlines()), 1); self.assertEqual(len(transport.calls), 1)
        code, status, _ = self.invoke(["ops-status", "--run-id", self.run_id, "--data-root", str(self.data), "--ledger", str(self.ledger), "--json"])
        self.assertEqual((code, status["state"]), (0, "SAFE_IDLE"))
        code, audit, _ = self.invoke(["audit-run", self.run_id, "--data-root", str(self.data), "--ledger", str(self.ledger), "--json"])
        self.assertEqual((code, audit["status"]), (0, "complete"), audit)
        code, health, _ = self.invoke(["ops-health", "--data-root", str(self.data), "--ledger", str(self.ledger), "--json"])
        self.assertEqual((code, health["status"]), (0, "HEALTHY"))

    def test_cli_exit_codes_artifact_failures_and_unavailable_reads(self):
        code, _, _ = self.invoke(["audit-run", self.run_id, "--data-root", str(self.data), "--ledger", str(self.ledger), "--json"])
        self.assertEqual(code, EXIT_BLOCKED)
        self.approve()
        with patch("btc_dca_bridge.cli._private_read_client_factory", side_effect=PrivateApiUnavailableError("offline")):
            code, _, _ = self.invoke(self.exact_args("reconcile-existing", self.recovery_run))
        self.assertEqual(code, EXIT_UNAVAILABLE)
        corrupt = self.data / "canary_manifests" / self.now.strftime("%Y/%m/%d") / f"{self.run_id}.json"
        corrupt.write_text("not-json\n")
        code, health, _ = self.invoke(["ops-health", "--data-root", str(self.data), "--ledger", str(self.ledger), "--json"])
        self.assertEqual((code, health["status"]), (EXIT_CORRUPT, "CORRUPT"))
        self.assertEqual(code, EXIT_CORRUPT)

    def test_lock_stale_recovery_is_explicit_and_audit_events_are_immutable(self):
        lock = OperationLock(self.data, client_order_id="dca-" + "a" * 32, approval_id="approval-" + "a" * 32, canary_id="canary-" + "a" * 32, now=lambda: self.now)
        lock.acquire()
        with self.assertRaises(OperationLockError): lock.recover_stale()
        payload = json.loads(lock.path.read_text()); payload["created_at_utc"] = (self.now - timedelta(minutes=16)).isoformat().replace("+00:00", "Z"); lock.path.write_text(json.dumps(payload))
        self.assertTrue(lock.recover_stale())
        service = OperationsService(data_root=self.data, ledger_path=self.ledger, now=lambda: self.now)
        paths = [service.record_event(action=action, result="blocked", reason="synthetic", run_id=self.run_id, artifact_ids={}) for action in ("operation_blocked", "reconciliation_requested", "integrity_failure")]
        self.assertEqual(len({path.name for path in paths}), 3)
        self.assertTrue(all("secret" not in path.read_text().lower() for path in paths))


if __name__ == "__main__": unittest.main()
