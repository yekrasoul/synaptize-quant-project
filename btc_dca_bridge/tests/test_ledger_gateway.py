from decimal import Decimal

from btc_dca_bridge.ledger import confirmed_executions, read_executions
from btc_dca_bridge.ledger_gateway import LedgerGateway


def test_record_is_idempotent_across_interfaces(tmp_path):
    ledger = tmp_path / "executions.jsonl"
    gateway = LedgerGateway(ledger_path=ledger)

    first_id, first_created = gateway.record_execution(
        executed_at_utc="2026-10-09T12:00:00Z",
        executed_usd=Decimal("25"),
        reference_price_usdt=Decimal("82000"),
        source="Chat 03 — Portfolio & Budget Tracker",
        note="confirmed",
    )
    second_id, second_created = gateway.record_execution(
        executed_at_utc="2026-10-09T12:00:00Z",
        executed_usd=Decimal("25"),
        reference_price_usdt=Decimal("82000"),
        source="This project chat",
        note="same confirmed fill",
    )

    assert first_id == second_id
    assert first_created is True
    assert second_created is False
    assert len(read_executions(ledger)) == 1


def test_correction_supersedes_without_double_counting(tmp_path):
    ledger = tmp_path / "executions.jsonl"
    gateway = LedgerGateway(ledger_path=ledger)

    original_id, _ = gateway.record_execution(
        executed_at_utc="2026-10-09T12:00:00Z",
        executed_usd=Decimal("25"),
        reference_price_usdt=Decimal("82000"),
        source="Chat 03 — Portfolio & Budget Tracker",
        note="initial",
    )
    replacement_id, _ = gateway.correct_execution(
        supersedes_execution_id=original_id,
        executed_at_utc="2026-10-09T12:00:00Z",
        executed_usd=Decimal("30"),
        reference_price_usdt=Decimal("81900"),
        source="Project chat correction",
        note="corrected amount and price",
    )

    active = confirmed_executions(read_executions(ledger))
    assert [item.execution_id for item in active] == [replacement_id]
    assert active[0].executed_usd == Decimal("30")


def test_cancel_removes_execution_from_active_projection(tmp_path):
    ledger = tmp_path / "executions.jsonl"
    gateway = LedgerGateway(ledger_path=ledger)

    original_id, _ = gateway.record_execution(
        executed_at_utc="2026-10-09T12:00:00Z",
        executed_usd=Decimal("25"),
        reference_price_usdt=Decimal("82000"),
        source="Any BTC DCA project chat",
        note="confirmed",
    )
    gateway.cancel_execution(
        supersedes_execution_id=original_id,
        cancelled_at_utc="2026-10-09T13:00:00Z",
        source="Any BTC DCA project chat",
        note="user corrected: purchase did not happen",
    )

    assert confirmed_executions(read_executions(ledger)) == ()
