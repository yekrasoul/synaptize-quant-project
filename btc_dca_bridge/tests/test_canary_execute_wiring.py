import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from btc_dca_bridge.cli import main


class RetiredCanaryExecuteTests(unittest.TestCase):
    def test_canary_execute_is_fail_closed_and_has_no_writer_dependencies(self):
        output = StringIO()
        with (
            patch("btc_dca_bridge.cli._submission_transport_factory") as transport,
            patch("btc_dca_bridge.cli._private_read_client_factory") as private_client,
            patch("btc_dca_bridge.cli._load_controlled_execution_artifacts") as artifacts,
            redirect_stdout(output),
        ):
            code = main(
                [
                    "canary-execute",
                    "--run-id", "run_20261025T220000Z_retired",
                    "--canary-id", "canary-retired",
                    "--approval-id", "approval-retired",
                    "--manifest-sha", "a" * 64,
                    "--approval-sha", "b" * 64,
                    "--month", "2026-10",
                ]
            )
        self.assertEqual(code, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["reason"], "LEGACY_WRITER_RETIRED")
        self.assertEqual(payload["outcome_category"], "request_not_sent")
        transport.assert_not_called()
        private_client.assert_not_called()
        artifacts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
