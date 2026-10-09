from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
from io import StringIO
from pathlib import Path

from btc_dca_bridge.cli import main
from btc_dca_bridge.ledger import confirmed_executions, read_executions


class LedgerCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.temp.name) / "executions.jsonl"
        self.ledger.write_text("")

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, json.loads(out.getvalue()) if out.getvalue() else {}, err.getvalue()

    def test_record_state_correct_cancel_cli_uses_gateway_projection(self):
        common = ["--ledger", str(self.ledger), "--month", "2026-10", "--json"]
        code, recorded, _ = self.invoke(["ledger-record", "--executed-at", "2026-10-09T12:00:00Z", "--usd", "25", "--reference-price", "82000", "--source", "Codex CLI", "--note", "user-confirmed", *common])
        self.assertEqual(code, 0)
        first_id = recorded["execution_id"]
        self.assertTrue(recorded["created"])

        code, state, _ = self.invoke(["ledger-state", "--month", "2026-10", "--ledger", str(self.ledger), "--json"])
        self.assertEqual((code, state["ledger_event_count"], state["active_execution_count"]), (0, 1, 1))
        self.assertEqual(state["portfolio_state"]["monthly_spent_usd"], 25)

        code, corrected, _ = self.invoke(["ledger-correct", "--target-execution-id", first_id, "--executed-at", "2026-10-09T12:00:00Z", "--usd", "30", "--reference-price", "81000", "--source", "Codex CLI", "--note", "corrected", *common])
        self.assertEqual(code, 0)
        corrected_id = corrected["execution_id"]
        self.assertEqual((corrected["ledger_event_count"], corrected["active_execution_count"], corrected["portfolio_state"]["monthly_spent_usd"]), (2, 1, 30))

        code, cancelled, _ = self.invoke(["ledger-cancel", "--target-execution-id", corrected_id, "--cancelled-at", "2026-10-09T13:00:00Z", "--source", "Codex CLI", "--note", "confirmed not executed", *common])
        self.assertEqual(code, 0)
        self.assertEqual((cancelled["ledger_event_count"], cancelled["active_execution_count"], cancelled["portfolio_state"]["monthly_spent_usd"]), (3, 0, 0))
        history = read_executions(self.ledger)
        self.assertEqual(len(history), 3)
        self.assertEqual(confirmed_executions(history), ())

    def test_distinct_purchase_cli_requires_explicit_identity_and_intent(self):
        base = ["--executed-at", "2026-10-09T12:00:00Z", "--usd", "25", "--reference-price", "82000", "--note", "confirmed", "--ledger", str(self.ledger), "--month", "2026-10", "--json"]
        self.assertEqual(self.invoke(["ledger-record", *base])[0], 0)
        different_time = ["--executed-at", "2026-10-09T12:01:00Z", "--usd", "25", "--reference-price", "82000", "--note", "separate", "--ledger", str(self.ledger), "--month", "2026-10", "--json"]
        code, _, _ = self.invoke(["ledger-record", *different_time])
        self.assertNotEqual(code, 0)
        code, result, _ = self.invoke(["ledger-record", *different_time, "--execution-id", "execution_manual_explicit_second", "--distinct-execution-intent"])
        self.assertEqual(code, 0)
        self.assertTrue(result["created"])
        self.assertEqual(len(confirmed_executions(read_executions(self.ledger))), 2)


if __name__ == "__main__":
    unittest.main()
