import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from btc_dca_bridge.operations import EXIT_RECONCILIATION, OperationLock, OperationLockError, OperationsService


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ledger = self.root / "executions.jsonl"
        self.ledger.write_text("")
        self.now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        self.service = OperationsService(data_root=self.root / "data", ledger_path=self.ledger, now=lambda: self.now)

    def test_empty_status_is_read_only_and_reports_safe_idle(self):
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot.state, "SAFE_IDLE")
        self.assertEqual(snapshot.allowed_actions, ("prepare",))
        self.assertFalse((self.root / "data").exists())
        self.assertEqual(snapshot.monthly_budget["remaining_usdt"], "500")

    def test_plan_health_and_audit_are_read_only_on_empty_store(self):
        self.assertEqual(self.service.health()["status"], "HEALTHY_BLOCKED_EXTERNAL_DEPENDENCY")
        audit = self.service.audit_run("run_20261007T120000Z_ops00001")
        self.assertEqual(audit["status"], "invalid")
        self.assertFalse((self.root / "data").exists())

    def test_operation_lock_blocks_duplicate_operator_and_releases(self):
        first = OperationLock(self.root / "data", client_order_id="dca-" + "a" * 32, approval_id="approval-" + "a" * 32, canary_id="canary-" + "a" * 32, now=lambda: self.now)
        second = OperationLock(self.root / "data", client_order_id="dca-" + "a" * 32, approval_id="approval-" + "a" * 32, canary_id="canary-" + "a" * 32, now=lambda: self.now)
        first.acquire()
        with self.assertRaises(OperationLockError): second.acquire()
        first.release()
        second.acquire(); second.release()

    def test_event_log_is_immutable_and_secret_free(self):
        path = self.service.record_event(action="plan_generated", result="ok", reason="read-only", run_id=None, artifact_ids={"client_order_id": None})
        self.assertTrue(path.exists())
        self.assertIn("manual_operator", path.read_text())


if __name__ == "__main__": unittest.main()
