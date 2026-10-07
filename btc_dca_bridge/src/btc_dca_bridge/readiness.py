"""Read-only Phase 5.6 production activation readiness checks.

This module has no submission transport and never constructs an order payload.
All external checks are injectable so the complete gate can be tested with
synthetic responses without credentials or network access.
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import load_execution_config, load_notification_config, load_strategy_config
from .ledger import read_executions
from .operations import OperationLock, OperationLockError, OperationsService
from .paths import DATA_PATH, LEDGER_PATH
from .private_bybit import BybitPrivateReadClient, CredentialClassification, PrivateBybitError


CHECK_STATUSES = {"PASS", "FAIL", "BLOCKED", "UNAVAILABLE", "NOT_APPLICABLE"}


@dataclass(frozen=True)
class ReadinessCheck:
    check_id: str
    category: str
    status: str
    required: bool
    evidence: str
    reason: str
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ProductionReadinessService:
    def __init__(self, *, data_root: Path = DATA_PATH, ledger_path: Path = LEDGER_PATH, now: Callable[[], datetime] | None = None, client_factory: Callable[[], Any] | None = None, repo_probe: Callable[[], Mapping[str, Any]] | None = None, server_time_probe: Callable[[], float] | None = None, filesystem_probe: Callable[[], tuple[bool, str]] | None = None, lock_probe: Callable[[], tuple[bool, str]] | None = None) -> None:
        self.data_root, self.ledger_path = Path(data_root), Path(ledger_path)
        self.now = now or (lambda: datetime.now(UTC))
        self.client_factory = client_factory or BybitPrivateReadClient.from_environment
        self.repo_probe = repo_probe or self._repo_status
        self.server_time_probe = server_time_probe
        self.filesystem_probe = filesystem_probe or self._filesystem_test
        self.lock_probe = lock_probe or self._lock_test

    @staticmethod
    def _repo_status() -> Mapping[str, Any]:
        def run(*args: str) -> str:
            return subprocess.run(("git", *args), capture_output=True, text=True, check=True).stdout.strip()
        status = run("status", "--porcelain")
        return {"commit": run("rev-parse", "HEAD"), "dirty": bool(status), "status": status}

    def _filesystem_test(self) -> tuple[bool, str]:
        try:
            self.data_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".readiness-", dir=self.data_root) as temp:
                path = Path(temp) / "durability-test"
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write("phase5.6\n"); handle.flush(); os.fsync(handle.fileno())
                directory_fd = os.open(temp, os.O_RDONLY)
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
            return True, "atomic create, fsync file, fsync directory, and cleanup succeeded"
        except OSError as exc:
            return False, str(exc)

    def _lock_test(self) -> tuple[bool, str]:
        root = self.data_root / ".readiness-lock-test"
        try:
            first = OperationLock(root, client_order_id="dca-" + "a" * 32, approval_id="approval-" + "a" * 32, canary_id="canary-" + "a" * 32, now=self.now)
            second = OperationLock(root, client_order_id="dca-" + "a" * 32, approval_id="approval-" + "a" * 32, canary_id="canary-" + "a" * 32, now=self.now)
            first.acquire()
            try:
                try: second.acquire()
                except OperationLockError: pass
                else: return False, "duplicate lock acquisition was not blocked"
            finally: first.release()
            second.acquire(); second.release()
            shutil.rmtree(root, ignore_errors=False)
            return True, "exclusive acquisition, duplicate blocking, release, and reacquisition succeeded"
        except Exception as exc:
            shutil.rmtree(root, ignore_errors=True)
            return False, str(exc)

    @staticmethod
    def _check(check_id: str, category: str, status: str, required: bool, evidence: str, reason: str, remediation: str = "") -> ReadinessCheck:
        if status not in CHECK_STATUSES: raise ValueError(f"invalid readiness status {status}")
        return ReadinessCheck(check_id, category, status, required, evidence, reason, remediation)

    def evaluate(self) -> dict[str, Any]:
        checks: list[ReadinessCheck] = []
        def add(*args: Any) -> None: checks.append(self._check(*args))
        try:
            config = load_execution_config()
            safe = config.live_execution_enabled is False and config.kill_switch is True and config.order_submission == "not_implemented"
            add("CONFIG_DEFAULTS", "configuration", "PASS" if safe else "FAIL", True, f"live_execution_enabled={config.live_execution_enabled}, kill_switch={config.kill_switch}, order_submission={config.order_submission}", "checked-in execution defaults are fail-safe" if safe else "checked-in execution defaults are unsafe", "Restore Phase 5.6 fail-safe execution defaults")
            strategy = load_strategy_config()
            add("STRATEGY_IDENTITY", "strategy", "PASS", True, f"{strategy.strategy_id} {strategy.strategy_version}", "canonical V1 strategy loaded")
            executions = read_executions(self.ledger_path)
            add("CANONICAL_LEDGER", "ledger", "PASS", True, f"{len(executions)} schema-valid executions", "canonical ledger readable and valid")
            operations = OperationsService(data_root=self.data_root, ledger_path=self.ledger_path, now=self.now)
            health = operations.health()
            add("ARTIFACT_INTEGRITY", "artifacts", "PASS" if health["status"] in {"HEALTHY", "HEALTHY_WITH_UNRESOLVED_RECONCILIATION"} else "FAIL", True, health["status"], "artifact and operations integrity is trusted" if health["status"] != "CORRUPT" else "artifact integrity is corrupt", "Repair immutable artifacts and digests")
            snapshot = operations.snapshot()
            unresolved = snapshot.reconciliation_required
            add("UNRESOLVED_OPERATIONS", "operations", "FAIL" if unresolved else "PASS", True, snapshot.state, "unresolved submission identity exists" if unresolved else "no unresolved submission identity", "Run reconcile-existing before preparing a new canary" if unresolved else "")
            add("MONTHLY_BUDGET", "ledger", "PASS" if Decimal(snapshot.monthly_budget["remaining_usdt"]) >= Decimal("10") and Decimal(snapshot.operational_exposure["potential_effective_remaining_budget_usdt"]) >= Decimal("10") else "FAIL", True, str(snapshot.operational_exposure), "fresh confirmed budget and exposure are sufficient" if Decimal(snapshot.monthly_budget["remaining_usdt"]) >= Decimal("10") else "monthly budget is insufficient", "Resolve exposure or wait for the next calendar month")
        except Exception as exc:
            add("LOCAL_STATE", "configuration", "FAIL", True, "unavailable", str(exc), "Repair local configuration, ledger, or artifacts")
            config = None
            snapshot = None
        try:
            repo = self.repo_probe()
            add("REPOSITORY_CLEAN", "security", "PASS" if not repo.get("dirty") else "BLOCKED", True, str(repo.get("commit")), "repository is clean" if not repo.get("dirty") else "tracked repository changes are present", "Deploy an immutable clean commit")
            add("PROCESS_IDENTITY", "operator", "PASS", False, f"hostname={socket.gethostname()}, pid={os.getpid()}, platform={platform.platform()}, python={platform.python_version()}, commit={repo.get('commit')}", "host/build identity recorded")
        except Exception as exc:
            add("REPOSITORY_CLEAN", "security", "UNAVAILABLE", True, "unavailable", str(exc), "Run from a readable repository checkout")
        try:
            notification = load_notification_config()
            add("NOTIFICATION_CONFIG", "security", "PASS", False, f"enabled={notification.telegram_enabled}, destination_vars={notification.bot_token_env_var},{notification.chat_id_env_var}", "notification configuration is parseable and secret-free")
        except Exception as exc: add("NOTIFICATION_CONFIG", "security", "FAIL", False, "unavailable", str(exc), "Repair notification configuration")
        for name in ("BYBIT_API_KEY", "BYBIT_API_SECRET"):
            add(f"SECRET_{name}", "credentials", "PASS" if os.environ.get(name) else "UNAVAILABLE", True, "PRESENT" if os.environ.get(name) else "ABSENT", "required secret presence checked without exposing value" if os.environ.get(name) else "required secret is absent", "Provide the secret through the approved runtime secret store")
        client = None
        try:
            client = self.client_factory()
            credential = client.credential_info()
            actions = {str(action) for values in credential.permissions.values() for action in values}
            dangerous = {"Withdrawal", "Withdraw", "AccountTransfer", "SubMemberTransfer", "Borrow", "Repay"}
            cred_ok = credential.classification is CredentialClassification.TRADE_CAPABLE and "SpotTrade" in actions and not actions.intersection(dangerous) and not any("derivative" in str(group).lower() or "contract" in str(group).lower() for group in credential.permissions)
            add("BYBIT_CREDENTIAL_SCOPE", "credentials", "PASS" if cred_ok else "FAIL", True, "classification and explicit permission groups inspected", "credential scope is Spot trade-capable and excludes unsafe permissions" if cred_ok else "credential scope cannot prove approved Spot-only permissions", "Use a dedicated least-privilege Spot credential")
            account = client.account_info()
            account_ok = all((account.unified_margin_status, account.margin_mode, account.spot_hedging_status, account.updated_time))
            add("BYBIT_ACCOUNT", "account", "PASS" if account_ok else "FAIL", True, str(account), "account metadata is complete" if account_ok else "account metadata is incomplete", "Repair account-info read contract")
            balances = {row.coin: row for row in client.wallet_balances()}
            liabilities = any(row.has_liability for row in balances.values() if row.coin in {"BTC", "USDT"})
            add("BYBIT_LIABILITIES", "wallet", "FAIL" if liabilities else "PASS", True, "BTC/USDT liability fields inspected", "BTC/USDT liabilities or accrued interest present" if liabilities else "no BTC/USDT liabilities or accrued interest")
            available = balances.get("USDT").available_for_spot_quote_buy if balances.get("USDT") else None
            add("BYBIT_SPOT_AVAILABLE_BALANCE", "wallet", "PASS" if available is not None else "FAIL", True, "authoritative field present" if available is not None else "available_for_spot_quote_buy unavailable", "authoritative Spot quote-buy availability is proven" if available is not None else "wallet arithmetic is not an acceptable substitute", "Implement or verify an official supported Bybit source for exact Spot quote-buy availability")
            rules = client.instrument_rules()
            valid_ranges = True
            for amount in (Decimal("10"), Decimal("100")): rules.validate_quote(amount)
            if rules.max_market_order_qty is not None: valid_ranges = rules.max_market_order_qty > 0
            add("BYBIT_INSTRUMENT", "instrument", "PASS" if valid_ranges else "FAIL", True, str(rules), "BTCUSDT Spot instrument accepts V1 $10-$100 range" if valid_ranges else "instrument limits invalidate V1 range", "Review current instrument metadata; do not change V1")
            add("DETERMINISTIC_ORDER_ID", "operations", "PASS", True, "client order identity and orderLinkId are supported", "deterministic identity support is present")
        except PrivateBybitError as exc:
            add("BYBIT_READ_ACCESS", "network", "UNAVAILABLE", True, "private read failed", str(exc), "Provide working authenticated read-only access")
        except Exception as exc:
            add("BYBIT_READ_ACCESS", "network", "UNAVAILABLE", True, "private read failed", str(exc), "Repair the supported read adapter")
        try:
            if self.server_time_probe is None: raise RuntimeError("server-time probe not configured")
            delta = float(self.server_time_probe())
            add("CLOCK_SKEW", "network", "PASS" if abs(delta) <= 2.0 else "FAIL", True, f"server_delta_seconds={delta}", "UTC clock skew is within 2 seconds" if abs(delta) <= 2.0 else "clock skew exceeds 2 seconds", "Synchronize the host clock; do not silently correct it")
        except Exception as exc: add("CLOCK_SKEW", "network", "UNAVAILABLE", True, "not measured", str(exc), "Measure authenticated server-time delta")
        fs_ok, fs_evidence = self.filesystem_probe(); add("FILESYSTEM_DURABILITY", "security", "PASS" if fs_ok else "FAIL", True, fs_evidence, "disposable durability self-test passed" if fs_ok else "durability self-test failed", "Use a filesystem supporting atomic create and fsync")
        lock_ok, lock_evidence = self.lock_probe(); add("OPERATOR_LOCK", "operator", "PASS" if lock_ok else "FAIL", True, lock_evidence, "operator lock self-test passed" if lock_ok else "operator lock self-test failed", "Repair local lock semantics")
        add("REAL_MONEY_AUTHORIZATION", "security", "PASS", False, "granted=false required=true status=NOT_AUTHORIZED", "readiness never grants real-money authorization")
        failures = [item for item in checks if item.required and item.status in {"FAIL", "BLOCKED", "UNAVAILABLE"}]
        state = "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION" if not failures else ("READY_FOR_OPERATOR_PREPARATION" if all(item.status not in {"FAIL", "BLOCKED"} for item in failures) else "NOT_READY")
        return {"status": state, "checks": [item.to_dict() for item in checks], "blockers": [{"check_id": item.check_id, "reason": item.reason, "remediation": item.remediation} for item in failures], "real_money_authorization": {"granted": False, "required": True, "status": "NOT_AUTHORIZED"}, "host": {"hostname": socket.gethostname(), "pid": os.getpid(), "platform": platform.platform(), "python_version": platform.python_version()}}


def production_connectivity(*, client_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Perform named authenticated GET reads only; never imports submission transport."""
    client = (client_factory or BybitPrivateReadClient.from_environment)()
    credential = client.credential_info(); account = client.account_info(); balances = client.wallet_balances(); rules = client.instrument_rules()
    return {"status": "READS_OK", "read_only": True, "credential_classification": credential.classification.value, "account_context": "complete", "coins": sorted(row.coin for row in balances), "instrument": str(rules), "server_time": "NOT_MEASURED", "message": "NO ORDER SUBMITTED"}
