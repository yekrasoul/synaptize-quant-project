import unittest

from btc_dca_bridge.ledger_sync import InMemoryVersionedLedgerStore, LedgerReconciliationService, LedgerVersionConflict


def row(identity, timestamp="2026-10-09T12:00:00Z"):
    return {"schema_version":"1.0.0", "execution_id":identity, "executed_at_utc":timestamp, "asset":"BTC", "quote_currency":"USDT", "executed_usd":25, "reference_price_usdt":80000, "status":"reconciled", "reconciliation":{"source":"Project conversation — user-confirmed execution", "note":"confirmed"}}


class InjectingConflictStore(InMemoryVersionedLedgerStore):
    def __init__(self):
        super().__init__(); self.inject=True
    def compare_and_swap(self, *, expected_version, new_content):
        if self.inject:
            self.inject=False
            snapshot=self.read()
            super().compare_and_swap(expected_version=snapshot.version, new_content=snapshot.content + (__import__("json").dumps(row("execution_other"), sort_keys=True, separators=(",", ":")).encode()+b"\n"))
        return super().compare_and_swap(expected_version=expected_version, new_content=new_content)


class InjectingSameOperationStore(InMemoryVersionedLedgerStore):
    def __init__(self, concurrent_row):
        super().__init__(); self.concurrent_row=concurrent_row; self.inject=True
    def compare_and_swap(self, *, expected_version, new_content):
        if self.inject:
            self.inject=False
            import json
            snapshot=self.read()
            line=json.dumps(self.concurrent_row, sort_keys=True, separators=(",", ":")).encode()+b"\n"
            super().compare_and_swap(expected_version=snapshot.version, new_content=snapshot.content+line)
        return super().compare_and_swap(expected_version=expected_version, new_content=new_content)


class LedgerSyncTests(unittest.TestCase):
    def test_compare_and_swap_success_and_append_only(self):
        store=InMemoryVersionedLedgerStore(); before=store.read()
        import json
        content=json.dumps(row("execution_one"), sort_keys=True, separators=(",", ":")).encode()+b"\n"
        store.compare_and_swap(expected_version=before.version, new_content=content)
        with self.assertRaises(LedgerVersionConflict): store.compare_and_swap(expected_version=before.version, new_content=b"")

    def test_stale_writer_retries_against_latest_without_erasing_other_event(self):
        store=InjectingConflictStore(); service=LedgerReconciliationService(store)
        wanted=row("execution_wanted")
        def builder(history, active):
            if any(item.execution_id == wanted["execution_id"] for item in history): return None
            return wanted
        result=service.apply(builder)
        self.assertTrue(result.created)
        from btc_dca_bridge.ledger import parse_executions
        self.assertEqual({item.execution_id for item in parse_executions(store.read().content)}, {"execution_other", "execution_wanted"})

    def test_replay_after_concurrent_semantic_write_is_idempotent(self):
        store=InMemoryVersionedLedgerStore(); service=LedgerReconciliationService(store)
        wanted=row("execution_same")
        def builder(history, active):
            if any(item.execution_id == "execution_same" for item in history): return None
            return wanted
        self.assertTrue(service.apply(builder).created)
        result=service.apply(builder)
        self.assertFalse(result.created)
        self.assertEqual(result.event_count, 1)

    def test_same_execution_written_by_other_interface_during_cas_is_idempotent(self):
        import json
        shared=row("execution_shared")
        store=InjectingSameOperationStore(shared); service=LedgerReconciliationService(store)
        def builder(history, active):
            existing=next((item.payload for item in history if item.execution_id=="execution_shared"), None)
            return existing or shared
        result=service.apply(builder)
        self.assertFalse(result.created)
        self.assertEqual(result.payload["execution_id"], "execution_shared")
        from btc_dca_bridge.ledger import parse_executions
        self.assertEqual(len(parse_executions(store.read().content)), 1)

    def test_conflicting_identity_does_not_merge(self):
        import json
        store=InMemoryVersionedLedgerStore(json.dumps(row("execution_same"), sort_keys=True, separators=(",", ":")).encode()+b"\n")
        service=LedgerReconciliationService(store)
        conflict=row("execution_same"); conflict["executed_usd"]=26
        with self.assertRaises(Exception):
            service.apply(lambda history, active: conflict)


if __name__ == "__main__": unittest.main()
