import hashlib
import http.client
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from btc_dca_bridge.artifacts import ArtifactStore, ArtifactType
from btc_dca_bridge.canary import CanaryPreparer
from btc_dca_bridge.cli import main
from btc_dca_bridge.config import load_execution_config
from btc_dca_bridge.execution import InstrumentRules
from btc_dca_bridge.live_activation import create_execution_activation
from btc_dca_bridge.live_readiness import (
    LiveExecutionApproval,
    account_identity_fingerprint,
    canonical_fingerprint,
    execution_safety_config_fingerprint,
    instrument_execution_projection,
    manifest_instrument_execution_projection,
    production_contract_fingerprint,
)
from btc_dca_bridge.market_data.http import HttpResponse
from btc_dca_bridge.operations import EXIT_RECONCILIATION
from btc_dca_bridge.private_bybit import (
    AccountInfo,
    ApiCredentialInfo,
    CredentialClassification,
    SpotQuoteAvailability,
    WalletBalance,
)
from btc_dca_bridge.production_enablement import (
    ENABLEMENT_CONFIRMATION,
    prepare_production_enablement,
)
from btc_dca_bridge.real_money_authorization import authorize_persisted_approval


REPOSITORY = Path(__file__).resolve().parents[1]


class ReadOnlyExecutionContext:
    identity = "mocked-e2e-api-key-identity"

    def credential_info(self):
        return ApiCredentialInfo(
            CredentialClassification.TRADE_CAPABLE,
            False,
            {"Spot": ("SpotTrade",), "Wallet": ("WalletRead",)},
            identity=self.identity,
        )

    def account_info(self):
        return AccountInfo("6", "REGULAR_MARGIN", "OFF", str(int(time.time() * 1000)))

    def wallet_balances(self):
        return (
            WalletBalance("USDT", Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("100")),
            WalletBalance("BTC", Decimal("0.01"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1000")),
        )

    def spot_quote_availability(self):
        return SpotQuoteAvailability(
            Decimal("100"), "/v5/order/spot-borrow-check", "spotMaxTradeAmount",
            "UNIFIED", True, datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )

    def instrument_rules(self):
        return InstrumentRules(
            Decimal("10"), Decimal("0.00001"), Decimal("0.00001"), Decimal("0.01"),
            quote_precision=Decimal("0.01"), max_market_order_qty=Decimal("100"),
            max_limit_order_qty=Decimal("50"), market_order_qty_unit="baseCoin",
        )

    def submission_state(self, client_order_id):
        return "conclusively_absent"

    def server_time_ms(self):
        return int(time.time() * 1000)


class HardBlockedTransport:
    def __init__(self):
        self.payloads = []

    def submit_spot_market_buy(self, request):
        self.payloads.append(request.to_payload())
        return HttpResponse(400, {"x-test-transport": "hard-blocked"}, b'{"retCode":10001,"retMsg":"blocked by test transport"}')


class CanaryExecuteEndToEndTests(unittest.TestCase):
    def _fresh_evidence(self, store, *, now, commit, account_fingerprint):
        paths = sorted((REPOSITORY / "data" / "production_evidence").glob("*/*/*/*.json"))
        self.assertTrue(paths, "repository must contain a production-evidence schema fixture")
        template = json.loads(paths[-1].read_text(encoding="utf-8"))
        stamp = now.isoformat().replace("+00:00", "Z")
        expires = (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        seed = hashlib.sha256(f"{stamp}:{commit}:e2e".encode()).hexdigest()[:32]
        evidence = dict(template)
        evidence.update(
            evidence_id=f"evidence-{seed}", created_at_utc=stamp, expires_at_utc=expires,
            repository_commit=commit, account_identity_fingerprint=account_fingerprint,
            account_identity_status="PROVEN",
            readiness_status="READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION",
            status="EVIDENCE_COMPLETE_READY_FOR_SEPARATE_AUTHORIZATION",
            wallet_observed_at_utc=stamp, availability_observed_at_utc=stamp,
            liability_observed_at_utc=stamp, clock_observed_at_utc=stamp,
            account_observed_at_utc=stamp, instrument_observed_at_utc=stamp,
        )
        evidence["check_results"] = [
            {**item, "status": "PASS"} if item.get("required") else dict(item)
            for item in evidence["check_results"]
        ]
        for key in (
            "clock_result", "credential_scope_result", "account_mode_result", "liability_result",
            "instrument_result", "spot_availability_result", "monthly_budget_result",
            "unresolved_operations_result", "secret_hygiene_result", "filesystem_result",
            "operator_lock_result",
        ):
            evidence[key] = {**evidence[key], "status": "PASS"}
        evidence["connectivity_result"] = {
            **evidence["connectivity_result"],
            "status": "READS_OK",
            "endpoints": [
                {**endpoint, "status": "PASS", "observed_at_utc": stamp, "response_contract_valid": True}
                for endpoint in evidence["connectivity_result"]["endpoints"]
            ],
        }
        receipt = store.persist_production_evidence(evidence)
        return evidence, receipt

    def test_real_cli_parser_to_hard_blocked_transport_exactly_once(self):
        now = datetime.now(UTC).replace(microsecond=0)
        month = now.strftime("%Y-%m")
        run_id = f"run_{now.strftime('%Y%m%dT%H%M%SZ')}_e2eproof"
        commit = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=REPOSITORY, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        reader = ReadOnlyExecutionContext()
        account_fingerprint = account_identity_fingerprint(
            exchange="Bybit", identity=reader.identity, unified_margin_status="6",
            margin_mode="REGULAR_MARGIN", spot_hedging_status="OFF",
        )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data_root = root / "data"
            ledger = root / "executions.jsonl"
            shutil.copyfile(REPOSITORY / "ledger" / "executions.jsonl", ledger)
            canonical_ledger_before = (REPOSITORY / "ledger" / "executions.jsonl").read_bytes()
            test_ledger_before = ledger.read_bytes()
            store = ArtifactStore(data_root)

            decision = json.loads((REPOSITORY / "data" / "decisions" / "2026" / "10" / "10" / "run_20261010T204000Z_0798546dbec5.json").read_text(encoding="utf-8"))
            decision.update(
                decision_id="decision_e2e_cli_boundary", created_at_utc=now.isoformat().replace("+00:00", "Z"),
                final_purchase_usd=10, monthly_spent_before_usd=95, remaining_budget_before_usd=405,
            )
            decision_receipt = store.persist(ArtifactType.DECISION, decision, run_id=run_id)
            prepared = CanaryPreparer(
                artifact_store=store, client=reader, now=lambda: now,
            ).prepare(decision, run_id=run_id, calendar_month=month, ledger_path=ledger)
            manifest = prepared.manifest.to_dict()
            intent, intent_sha = store.find_artifact(
                ArtifactType.ORDER_INTENT, identity_field="order_intent_id",
                identity_value=manifest["order_intent_id"],
            )
            evidence, evidence_receipt = self._fresh_evidence(
                store, now=now, commit=commit, account_fingerprint=account_fingerprint,
            )
            checks = {item["check_id"]: item for item in evidence["check_results"]}
            config = load_execution_config()
            approval = LiveExecutionApproval.prepare(
                now_utc=now, repository_commit_sha=commit,
                strategy_id=manifest["strategy_id"], strategy_version=manifest["strategy_version"],
                canary_manifest_id=manifest["canary_id"], canary_manifest_sha256=prepared.artifact_receipt.sha256,
                decision_id=manifest["decision_id"], decision_sha256=decision_receipt.sha256,
                order_intent_id=manifest["order_intent_id"], order_intent_sha256=intent_sha,
                client_order_id=manifest["client_order_id"], quote_amount_usdt="10",
                calendar_month=month, monthly_spent_usdt="95", remaining_monthly_budget_usdt="405",
                account_identity_fingerprint=account_fingerprint, account_mode="REGULAR_MARGIN",
                credential_scope_classification="SPOT_TRADE_NO_WITHDRAW", liability_state="NONE",
                spot_usdt_availability_fingerprint=canonical_fingerprint({
                    "authoritative_available_usdt": manifest["authoritative_available_usdt"],
                    "availability_method": manifest["availability_method"],
                }),
                instrument_metadata_fingerprint=canonical_fingerprint(manifest_instrument_execution_projection(manifest)),
                clock_skew_evidence_fingerprint=canonical_fingerprint(checks["CLOCK_SKEW"]),
                reconciliation_evidence_fingerprint=canonical_fingerprint({
                    "client_order_id": manifest["client_order_id"], "state": "conclusively_absent",
                }),
                production_contract_fingerprint=production_contract_fingerprint(),
                execution_safety_config_fingerprint=execution_safety_config_fingerprint(config),
                manifest_expires_at_utc=manifest["expires_at_utc"], state="PREPARED",
            )
            approval_receipt = approval.persist(store, run_id=run_id)
            authorization, authorization_receipt = authorize_persisted_approval(
                store=store, approval_id=approval.approval_id, approval_sha256=approval_receipt.sha256,
                canary_manifest_id=manifest["canary_id"], canary_manifest_sha256=prepared.artifact_receipt.sha256,
                amount="10", client_order_id=manifest["client_order_id"], calendar_month=month,
                confirmation=f"AUTHORIZE BYBIT SPOT BTCUSDT BUY $10 {manifest['client_order_id']}",
                run_id=run_id + "_auth", now_utc=now,
            )
            activation, activation_receipt = create_execution_activation(
                store=store, authorization_id=authorization.authorization_id,
                authorization_sha256=authorization_receipt.sha256, approval_id=approval.approval_id,
                approval_sha256=approval_receipt.sha256, client_order_id=manifest["client_order_id"],
                amount="10", calendar_month=month,
                phrase=f"ACTIVATE ONE SHOT BYBIT SPOT BTCUSDT BUY $10 {manifest['client_order_id']}",
                run_id=run_id + "_activation", now_utc=now,
            )
            enablement, enablement_receipt = prepare_production_enablement(
                store=store, evidence=evidence, repository_commit_sha=commit,
                execution_policy_fingerprint=execution_safety_config_fingerprint(config),
                production_contract_fingerprint=production_contract_fingerprint(),
                account_identity_fingerprint=account_fingerprint, account_mode="REGULAR_MARGIN",
                credential_scope="SPOT_TRADE_NO_WITHDRAW", confirmation=ENABLEMENT_CONFIRMATION,
                run_id=run_id + "_enablement", now_utc=now,
            )

            transport = HardBlockedTransport()
            output = StringIO()
            argv = [
                "canary-execute", "--run-id", run_id,
                "--canary-id", manifest["canary_id"], "--manifest-sha", prepared.artifact_receipt.sha256,
                "--approval-id", approval.approval_id, "--approval-sha", approval_receipt.sha256,
                "--authorization-id", authorization.authorization_id, "--authorization-sha", authorization_receipt.sha256,
                "--activation-id", activation.activation_id, "--activation-sha", activation_receipt.sha256,
                "--enablement-id", enablement.enablement_id, "--enablement-sha", enablement_receipt.sha256,
                "--evidence-id", evidence["evidence_id"], "--evidence-sha", evidence_receipt.sha256,
                "--data-root", str(data_root), "--ledger", str(ledger), "--month", month, "--json",
            ]
            with patch("btc_dca_bridge.cli._private_read_client_factory", return_value=reader), \
                 patch("btc_dca_bridge.cli._submission_transport_factory", return_value=transport) as transport_factory, \
                 patch("btc_dca_bridge.cli._post_ack_reconciler_factory", return_value=Mock()), \
                 patch.object(http.client, "HTTPSConnection") as real_network, redirect_stdout(output):
                code = main(argv)

            self.assertEqual(code, EXIT_RECONCILIATION, output.getvalue())
            self.assertEqual(len(transport.payloads), 1)
            self.assertEqual(transport.payloads[0], {
                "category": "spot", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market",
                "qty": "10", "marketUnit": "quoteCoin", "isLeverage": 0,
                "orderLinkId": manifest["client_order_id"], "orderFilter": "Order",
            })
            transport_factory.assert_called_once()
            real_network.assert_not_called()
            self.assertEqual(ledger.read_bytes(), test_ledger_before)
            self.assertEqual((REPOSITORY / "ledger" / "executions.jsonl").read_bytes(), canonical_ledger_before)
            self.assertEqual(len(list((data_root / "order_submission_attempts").rglob("*.json"))), 1)
            self.assertEqual(json.loads(output.getvalue())["outcome_category"], "exchange_rejected")
            self.assertEqual(
                canonical_fingerprint(instrument_execution_projection(reader.instrument_rules())),
                approval.payload["instrument_metadata_fingerprint"],
            )


if __name__ == "__main__":
    unittest.main()
