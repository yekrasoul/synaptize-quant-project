"""Read-only Phase 5.6 production activation readiness checks.

This module has no submission transport and never constructs an order payload.
All external checks are injectable so the complete gate can be tested with
synthetic responses without credentials or network access.
"""
from __future__ import annotations

import os
import json
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import load_execution_config, load_notification_config, load_strategy_config
from . import availability
from .ledger import read_executions
from .operations import OperationLock, OperationLockError, OperationsService
from .paths import DATA_PATH, LEDGER_PATH
from .private_bybit import AccountInfo, BybitPrivateReadClient, CredentialClassification, MalformedBybitResponseError, PrivateBybitError
from .quote_limits import QuoteUnitLimitEvidence, QuoteUnitLimitPolicy, QuoteUnitLimitValidationError, PRODUCTION_QUOTE_UNIT_LIMIT_POLICY, unavailable_quote_unit_limit, validate_quote_unit_limit_evidence


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


@dataclass(frozen=True)
class ServerTimeMeasurement:
    delta_seconds: float
    round_trip_ms: float
    threshold_seconds: float = 2.0


def measure_server_time(server_time_ms: Callable[[], int], *, clock: Callable[[], float] = time.time) -> ServerTimeMeasurement:
    started = clock()
    remote_ms = int(server_time_ms())
    finished = clock()
    midpoint = (started + finished) / 2
    delta = remote_ms / 1000 - midpoint
    return ServerTimeMeasurement(delta, max(0.0, (finished - started) * 1000))


def classify_production_account_mode(account: AccountInfo, *, now: datetime) -> tuple[bool, str]:
    """Accept supported UTA 2.0 account state from a live authenticated read.\n\n    Bybit updatedTime is when account data last changed, not this response\n    timestamp. It must parse and not be future-dated, but it need not be\n    recent. Live-read freshness is established by authenticated GET and\n    server-clock checks.\n    """
    status = str(account.unified_margin_status)
    if status not in {"5", "6"}:
        return False, "unsupported unifiedMarginStatus; only UTA 2.0 status 5 or UTA 2.0 Pro status 6 is supported"
    if account.margin_mode != "REGULAR_MARGIN":
        return False, "unsupported marginMode; only REGULAR_MARGIN is supported"
    if account.spot_hedging_status != "OFF":
        return False, "unsupported spotHedgingStatus; only OFF is supported"
    raw = account.updated_time
    try:
        if raw is None:
            raise ValueError("missing updatedTime")
        if str(raw).isdigit():
            observed = datetime.fromtimestamp(int(str(raw)) / 1000, tz=UTC)
        else:
            observed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise ValueError("updatedTime is not timezone-aware")
            observed = observed.astimezone(UTC)
        if observed > now + timedelta(seconds=2):
            return False, "account updatedTime is future-dated"
    except (TypeError, ValueError, OverflowError) as exc:
        return False, f"account updatedTime is invalid: {exc}"
    status_name = "UTA 2.0" if status == "5" else "UTA 2.0 Pro"
    return True, f"supported {status_name} status {status} / REGULAR_MARGIN / spotHedgingStatus OFF"


class ProductionReadinessService:
    def __init__(self, *, data_root: Path = DATA_PATH, ledger_path: Path = LEDGER_PATH, now: Callable[[], datetime] | None = None, client_factory: Callable[[], Any] | None = None, repo_probe: Callable[[], Mapping[str, Any]] | None = None, server_time_probe: Callable[[], Any] | None = None, filesystem_probe: Callable[[], tuple[bool, str]] | None = None, lock_probe: Callable[[], tuple[bool, str]] | None = None, secret_scan_probe: Callable[[], Mapping[str, Any]] | None = None, availability_policy: availability.SpotQuoteAvailabilityPolicy = availability.PRODUCTION_AVAILABILITY_POLICY, quote_limit_policy: QuoteUnitLimitPolicy = PRODUCTION_QUOTE_UNIT_LIMIT_POLICY) -> None:
        self.data_root, self.ledger_path = Path(data_root), Path(ledger_path)
        self.now = now or (lambda: datetime.now(UTC))
        self.client_factory = client_factory or BybitPrivateReadClient.from_environment
        self.repo_probe = repo_probe or self._repo_status
        self.server_time_probe = server_time_probe
        self.filesystem_probe = filesystem_probe or self._filesystem_test
        self.lock_probe = lock_probe or self._lock_test
        self.secret_scan_probe = secret_scan_probe or self._secret_hygiene
        self.availability_policy = availability_policy
        self.quote_limit_policy = quote_limit_policy

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
    def _secret_hygiene() -> Mapping[str, Any]:
        cwd = Path.cwd().resolve()
        root = cwd.parent if (cwd.name == "btc_dca_bridge" and (cwd.parent / ".git").exists()) else cwd
        candidates = [root, root / "btc_dca_bridge", root / "config", root / "data", root / "ledger"]
        candidates = list(dict.fromkeys(candidates))
        gitleaks = shutil.which("gitleaks")
        if gitleaks:
            result = subprocess.run((gitleaks, "detect", "--source", str(root), "--no-banner", "--redact"), capture_output=True, text=True)
            if result.returncode == 0:
                return {"scanner": "gitleaks", "scope": [str(root)], "status": "PASS", "finding_count": 0, "findings": []}
            if result.returncode == 1:
                return {"scanner": "gitleaks", "scope": [str(root)], "status": "FAIL", "finding_count": 1, "findings": [{"type": "redacted-finding"}]}
            return {"scanner": "gitleaks", "scope": [str(root)], "status": "UNAVAILABLE", "finding_count": 0, "findings": []}
        patterns = {
            "private_key": re.compile(r"-----BEGIN (?:RSA|OPENSSH|EC|DSA) PRIVATE KEY-----"),
            "access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
            "secret_assignment": re.compile(r"\b(?:BYBIT_API_SECRET|TELEGRAM_BOT_TOKEN)\s*[:=]\s*[A-Za-z0-9_:/+=-]{20,}") ,
        }
        findings: list[dict[str, str]] = []
        for base in candidates:
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if not path.is_file() or any(part in {".git", "__pycache__", ".venv", "graphify-out"} for part in path.parts):
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for kind, pattern in patterns.items():
                    if pattern.search(text):
                        findings.append({"path": str(path.relative_to(root)), "type": kind})
        return {"scanner": "fallback-regex", "scope": [str(path.relative_to(root)) for path in candidates if path.exists()], "status": "PASS" if not findings else "FAIL", "finding_count": len(findings), "findings": findings}

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
            from .ledger import confirmed_executions
            active = confirmed_executions(executions)
            add("CANONICAL_LEDGER", "ledger", "PASS", True, f"{len(executions)} historical events; {len(active)} active confirmed executions", "canonical ledger readable and active projection valid")
            operations = OperationsService(data_root=self.data_root, ledger_path=self.ledger_path, now=self.now)
            health = operations.health()
            add("ARTIFACT_INTEGRITY", "artifacts", "PASS" if health["status"] in {"HEALTHY", "HEALTHY_WITH_UNRESOLVED_RECONCILIATION", "HEALTHY_BLOCKED_EXTERNAL_DEPENDENCY"} else "FAIL", True, health["status"], "artifact and operations integrity is trusted" if health["status"] != "CORRUPT" else "artifact integrity is corrupt", "Repair immutable artifacts and digests")
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
            # _classify_permissions validates the complete shape, including
            # Bybit's Unified-account DerivativesTrade parent permission. It
            # must not be rejected here merely because its group is named
            # "Derivatives"; ContractTrade, Options, dangerous Wallet scopes,
            # and unknown non-empty groups remain blocked by that classifier.
            cred_ok = credential.classification is CredentialClassification.TRADE_CAPABLE and "SpotTrade" in actions and not actions.intersection(dangerous)
            add("BYBIT_CREDENTIAL_SCOPE", "credentials", "PASS" if cred_ok else "FAIL", True, "classification and explicit permission groups inspected", "credential scope is Spot trade-capable and excludes unsafe permissions" if cred_ok else "credential scope cannot prove approved Spot-only permissions", "Use a dedicated least-privilege Spot credential")
            account = client.account_info()
            account_ok, account_reason = classify_production_account_mode(account, now=self.now())
            add("BYBIT_ACCOUNT", "account", "PASS" if account_ok else "FAIL", True, str(account), account_reason, "Use UTA 2.0 status 5 or 6 with REGULAR_MARGIN / OFF and valid non-future account metadata")
            balances = {row.coin: row for row in client.wallet_balances()}
            liabilities = any(row.has_liability for row in balances.values() if row.coin in {"BTC", "USDT"})
            add("BYBIT_LIABILITIES", "wallet", "FAIL" if liabilities else "PASS", True, "BTC/USDT liability fields inspected", "BTC/USDT liabilities or accrued interest present" if liabilities else "no BTC/USDT liabilities or accrued interest")
            available = client.spot_quote_availability() if hasattr(client, "spot_quote_availability") else None
            try:
                available_amount = availability.validate_spot_quote_availability(available, now=self.now(), policy=self.availability_policy)
                if available_amount < Decimal("10"):
                    raise availability.AvailabilityValidationError(
                        f"authoritative Spot quote availability {available_amount} is below the V1 minimum of 10 USDT"
                    )
            except availability.AvailabilityValidationError as exc:
                add("BYBIT_SPOT_AVAILABLE_BALANCE", "wallet", "FAIL", True, "authoritative provenance unavailable", str(exc), "Implement and officially verify an exact Spot quote-buy availability source")
            else:
                add("BYBIT_SPOT_AVAILABLE_BALANCE", "wallet", "PASS", True, f"authoritative_amount_usdt={available_amount}; minimum_v1_usdt=10", "authoritative Spot quote-buy availability is proven and meets the V1 minimum", "")
            rules = client.instrument_rules()
            observed_at = self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
            try:
                quote_limit_evidence = client.quote_unit_limit_evidence()
            except Exception as exc:
                quote_limit_evidence = unavailable_quote_unit_limit(observed_at_utc=observed_at)
                quote_limit_error = str(exc)
            else:
                quote_limit_error = ""
            v1_results: dict[str, str] = {}
            try:
                quote_maximum = validate_quote_unit_limit_evidence(quote_limit_evidence, now=self.now(), policy=self.quote_limit_policy)
            except QuoteUnitLimitValidationError as exc:
                for amount in (Decimal("10"), Decimal("25"), Decimal("50"), Decimal("75"), Decimal("100")):
                    v1_results[str(amount)] = f"UNAVAILABLE: {exc}"
            else:
                for amount in (Decimal("10"), Decimal("25"), Decimal("50"), Decimal("75"), Decimal("100")):
                    try:
                        rules.validate_quote(amount)
                        if amount > quote_maximum: raise ValueError("quote amount exceeds authoritative quoteCoin market-buy maximum")
                        v1_results[str(amount)] = "PASS"
                    except Exception as exc:
                        v1_results[str(amount)] = f"UNAVAILABLE: {exc}"
            valid_ranges = all(value == "PASS" for value in v1_results.values())
            add("BYBIT_INSTRUMENT", "instrument", "PASS" if valid_ranges else "UNAVAILABLE", True, str(v1_results), "BTCUSDT Spot quoteCoin contract proves V1 $10-$100" if valid_ranges else (quote_limit_error or "quoteCoin market-buy upper bound cannot be proven from current authoritative fields"), "Implement an official quote-unit upper-bound source; PROPOSED V2 CHANGE REQUIRED if V1 limits must change")
            add("DETERMINISTIC_ORDER_ID", "operations", "PASS", True, "client order identity and orderLinkId are supported", "deterministic identity support is present")
        except PrivateBybitError as exc:
            add("BYBIT_READ_ACCESS", "network", "UNAVAILABLE", True, "private read failed", str(exc), "Provide working authenticated read-only access")
        except Exception as exc:
            add("BYBIT_READ_ACCESS", "network", "UNAVAILABLE", True, "private read failed", str(exc), "Repair the supported read adapter")
        try:
            if self.server_time_probe is not None:
                measurement = self.server_time_probe()
                measurement = measurement if isinstance(measurement, ServerTimeMeasurement) else ServerTimeMeasurement(float(measurement["delta_seconds"]), float(measurement["round_trip_ms"]))
            elif client is not None:
                measurement = measure_server_time(client.server_time_ms)
            else:
                raise RuntimeError("server-time probe unavailable")
            add("CLOCK_SKEW", "network", "PASS" if abs(measurement.delta_seconds) <= measurement.threshold_seconds else "FAIL", True, f"server_delta_seconds={measurement.delta_seconds}, round_trip_ms={measurement.round_trip_ms}, threshold_seconds={measurement.threshold_seconds}", "UTC clock skew is within the documented threshold" if abs(measurement.delta_seconds) <= measurement.threshold_seconds else "clock skew exceeds the documented threshold", "Synchronize the host clock; do not silently correct it")
        except Exception as exc: add("CLOCK_SKEW", "network", "UNAVAILABLE", True, "not measured", str(exc), "Measure authenticated server-time delta")
        try:
            if client is None:
                raise RuntimeError("authenticated read client unavailable")
            connectivity = production_connectivity(client_factory=lambda: client)
            connectivity_status = "PASS" if connectivity["status"] == "READS_OK" else connectivity["status"].replace("READS_", "")
            add("PRODUCTION_CONNECTIVITY", "network", connectivity_status, True, json.dumps(connectivity["endpoints"], sort_keys=True), "all production-critical GET paths are readable" if connectivity_status == "PASS" else "one or more production-critical GET paths are unavailable or malformed", "Repair authenticated GET access and response contracts")
        except Exception as exc:
            add("PRODUCTION_CONNECTIVITY", "network", "UNAVAILABLE", True, "connectivity checks unavailable", str(exc), "Run the read-only production connectivity check")
        try:
            secret = self.secret_scan_probe()
            add("SECRET_HYGIENE", "security", str(secret.get("status", "UNAVAILABLE")), True, json.dumps({key: secret.get(key) for key in ("scanner", "scope", "finding_count")}, sort_keys=True), "repository/config/artifact secret scan is clean" if secret.get("status") == "PASS" else "secret scan found material or could not complete", "Remove secret material or repair the scanner")
        except Exception as exc:
            add("SECRET_HYGIENE", "security", "UNAVAILABLE", True, "scanner unavailable", str(exc), "Run gitleaks or the approved fallback scanner")
        fs_ok, fs_evidence = self.filesystem_probe(); add("FILESYSTEM_DURABILITY", "security", "PASS" if fs_ok else "FAIL", True, fs_evidence, "disposable durability self-test passed" if fs_ok else "durability self-test failed", "Use a filesystem supporting atomic create and fsync")
        lock_ok, lock_evidence = self.lock_probe(); add("OPERATOR_LOCK", "operator", "PASS" if lock_ok else "FAIL", True, lock_evidence, "operator lock self-test passed" if lock_ok else "operator lock self-test failed", "Repair local lock semantics")
        add("REAL_MONEY_AUTHORIZATION", "security", "PASS", False, "granted=false required=true status=NOT_AUTHORIZED", "readiness never grants real-money authorization")
        failures = [item for item in checks if item.required and item.status in {"FAIL", "BLOCKED", "UNAVAILABLE"}]
        state = "READY_FOR_SEPARATE_REAL_MONEY_AUTHORIZATION" if not failures else ("READY_FOR_OPERATOR_PREPARATION" if all(item.status not in {"FAIL", "BLOCKED"} for item in failures) else "NOT_READY")
        return {"status": state, "checks": [item.to_dict() for item in checks], "blockers": [{"check_id": item.check_id, "reason": item.reason, "remediation": item.remediation} for item in failures], "quote_unit_limit_evidence": quote_limit_evidence.to_dict() if 'quote_limit_evidence' in locals() else unavailable_quote_unit_limit(observed_at_utc=self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")).to_dict(), "real_money_authorization": {"granted": False, "required": True, "status": "NOT_AUTHORIZED"}, "host": {"hostname": socket.gethostname(), "pid": os.getpid(), "platform": platform.platform(), "python_version": platform.python_version()}}


def production_connectivity(*, client_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Perform named GET-only reads, including harmless empty order probes."""
    observed_at = lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    endpoints_required = ("credential_info", "account_info", "wallet_balance", "spot_quote_availability", "instrument_metadata", "server_time", "order_realtime", "order_history", "execution_list")
    try:
        client = (client_factory or BybitPrivateReadClient.from_environment)()
    except MalformedBybitResponseError as exc:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return {"status": "READS_FAILED", "read_only": True, "endpoints": [{"endpoint": name, "status": "FAIL", "reason": str(exc), "read_only": True, "observed_at_utc": timestamp, "response_contract_valid": False} for name in endpoints_required], "probe_order_link_id": "dca-readiness-probe-00000000000000000000000000000000", "message": "NO ORDER SUBMITTED"}
    except Exception as exc:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return {"status": "READS_UNAVAILABLE", "read_only": True, "endpoints": [{"endpoint": name, "status": "UNAVAILABLE", "reason": str(exc), "read_only": True, "observed_at_utc": timestamp, "response_contract_valid": False} for name in endpoints_required], "probe_order_link_id": "dca-readiness-probe-00000000000000000000000000000000", "message": "NO ORDER SUBMITTED"}
    probe_id = "dca-readiness-probe-00000000000000000000000000000000"
    calls = {
        "credential_info": client.credential_info,
        "account_info": client.account_info,
        "wallet_balance": client.wallet_balances,
        "spot_quote_availability": client.spot_quote_availability,
        "instrument_metadata": client.instrument_rules,
        "server_time": client.server_time_ms,
        "order_realtime": lambda: client.order_realtime_probe(probe_id),
        "order_history": lambda: client.order_history_probe(probe_id),
        "execution_list": lambda: client.executions(probe_id),
    }
    endpoints: list[dict[str, Any]] = []
    for endpoint, operation in calls.items():
        try:
            operation()
        except MalformedBybitResponseError as exc:
            endpoints.append({"endpoint": endpoint, "status": "FAIL", "reason": str(exc), "read_only": True, "observed_at_utc": observed_at(), "response_contract_valid": False})
        except PrivateBybitError as exc:
            endpoints.append({"endpoint": endpoint, "status": "UNAVAILABLE", "reason": str(exc), "read_only": True, "observed_at_utc": observed_at(), "response_contract_valid": False})
        except Exception as exc:
            endpoints.append({"endpoint": endpoint, "status": "UNAVAILABLE", "reason": str(exc), "read_only": True, "observed_at_utc": observed_at(), "response_contract_valid": False})
        else:
            endpoints.append({"endpoint": endpoint, "status": "PASS", "reason": "GET read succeeded; empty/not-found is acceptable for probe identity", "read_only": True, "observed_at_utc": observed_at(), "response_contract_valid": True})
    statuses = {item["status"] for item in endpoints}
    status = "READS_FAILED" if "FAIL" in statuses else ("READS_UNAVAILABLE" if "UNAVAILABLE" in statuses else "READS_OK")
    return {"status": status, "read_only": True, "endpoints": endpoints, "probe_order_link_id": probe_id, "message": "NO ORDER SUBMITTED"}
