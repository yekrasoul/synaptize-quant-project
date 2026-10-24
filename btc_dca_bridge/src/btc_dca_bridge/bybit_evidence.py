"""Read-only, sanitized Bybit account evidence capture."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol

from .private_bybit import (
    AccountInfo,
    ApiCredentialInfo,
    BybitPrivateReadClient,
    WalletBalance,
    credential_scope_allows_spot_execution,
)


class BybitEvidenceReadPort(Protocol):
    def credential_info(self) -> ApiCredentialInfo: ...
    def account_info(self) -> AccountInfo: ...
    def wallet_balances(self) -> tuple[WalletBalance, ...]: ...
    def server_time_ms(self) -> int: ...


class BybitEvidenceReadAdapter:
    """Narrow capture boundary over the authenticated, GET-only Bybit client."""

    def __init__(self, client: BybitPrivateReadClient) -> None:
        self._client = client

    def credential_info(self) -> ApiCredentialInfo:
        return self._client.credential_info()

    def account_info(self) -> AccountInfo:
        return self._client.account_info()

    def wallet_balances(self) -> tuple[WalletBalance, ...]:
        return self._client.wallet_balances()

    def server_time_ms(self) -> int:
        return self._client.server_time_ms()


_SAFE_PERMISSION_GROUPS = frozenset({"Spot", "Wallet", "Derivatives", "ContractTrade", "Options"})
_SAFE_PERMISSION_ACTIONS = frozenset({
    "Read", "SpotRead", "WalletRead", "AccountRead", "Query", "SpotTrade",
    "DerivativesTrade", "OrderEntry", "SpotOrder", "Order", "Position", "OptionsTrade",
    "AccountTransfer", "SubMemberTransfer", "Withdrawal", "Withdraw", "Borrow", "Repay",
})


def _normalized_permission_scope(credential: ApiCredentialInfo) -> dict[str, object]:
    """Preserve only known permission labels; never serialize provider values verbatim."""
    groups: dict[str, list[str]] = {}
    for group, actions in credential.permissions.items():
        if group not in _SAFE_PERMISSION_GROUPS:
            groups["UNKNOWN"] = ["UNKNOWN"]
            continue
        groups[group] = [action if action in _SAFE_PERMISSION_ACTIONS else "UNKNOWN" for action in sorted(actions)]
    return {
        "classification": credential.classification.value,
        "read_only": credential.read_only,
        "groups": groups,
    }


def capture(
    client: BybitEvidenceReadPort,
    *,
    environment: str,
    observed_at: datetime | None = None,
    maximum_clock_skew_ms: int = 300_000,
) -> dict[str, Any]:
    """Uses only credential_info, account_info, wallet_balances and server_time_ms."""
    if maximum_clock_skew_ms < 0:
        raise ValueError("maximum_clock_skew_ms must be non-negative")
    credential, account, balances = client.credential_info(), client.account_info(), client.wallet_balances()
    usdt = next((row for row in balances if row.coin == "USDT"), None)
    captured_at = observed_at or datetime.now(UTC)
    if captured_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    exchange_time = client.server_time_ms()
    # Absence of a withdrawal token is not direct proof that withdrawal is
    # disabled; only a provider field with that exact meaning could promote it.
    withdrawal = "UNKNOWN"
    return {
        "schema_version": "1.0.0",
        "captured_at": captured_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "venue": "Bybit",
        "environment": environment or "UNKNOWN",
        "account_fingerprint": hashlib.sha256(str(account).encode()).hexdigest(),
        "subaccount_fingerprint": None,
        "account_type": str(account.unified_margin_status or "UNKNOWN"),
        "spot_suitable": credential_scope_allows_spot_execution(credential),
        "quote_asset": "USDT",
        "quote_balance": str(usdt.wallet_balance if usdt else "0"),
        "normalized_permission_scope": _normalized_permission_scope(credential),
        "withdrawal_permission_status": withdrawal,
        "exchange_time": exchange_time,
        "freshness_status": (
            "VERIFIED"
            if abs(exchange_time - int(captured_at.timestamp() * 1000)) <= maximum_clock_skew_ms
            else "NOT_VERIFIED"
        ),
    }
