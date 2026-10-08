import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from btc_dca_bridge.errors import (
    DataUnavailableError,
    ShadowRunAlreadyCompletedError,
    ShadowRunError,
    ShadowRunErrorCode,
)
from btc_dca_bridge.production import (
    ProductionRunContext,
    acquisition_minute,
    manual_run_id,
    run_production_shadow,
    scheduled_run_id,
    scheduled_slot,
)


ROOT = Path(__file__).resolve().parents[1]


class StructuredShadow:
    def to_dict(self):
        return {
            "run_id": "structured",
            "market_snapshot": {"source": "bybit_api"},
            "sentiment_snapshot": {"value": 35},
            "decision": {"final_purchase_usd": 98},
            "no_order_executed": True,
        }


class FakePipeline:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class ProductionShadowTest(unittest.TestCase):
    def setUp(self):
        self.started = datetime(2026, 10, 7, 12, 24, 19, tzinfo=UTC)
        self.context = ProductionRunContext.create(
            trigger_type="scheduled",
            process_started_at_utc=self.started,
            trigger_id="999",
        )

    def test_scheduled_slot_and_run_id_are_deterministic_and_minute_aligned(self):
        slot = scheduled_slot(self.started)
        self.assertEqual(slot, datetime(2026, 10, 7, 12, 23, tzinfo=UTC))
        self.assertEqual(slot.second, 0)
        self.assertEqual(scheduled_run_id(slot), scheduled_run_id(slot))
        self.assertTrue(scheduled_run_id(slot).startswith("run_20261007T122300Z_scheduled_"))
        before_slot = datetime(2026, 10, 7, 12, 22, 59, tzinfo=UTC)
        self.assertEqual(scheduled_slot(before_slot), datetime(2026, 10, 7, 6, 23, tzinfo=UTC))

    def test_manual_identity_is_isolated_from_schedule_and_other_dispatches(self):
        logical = datetime(2026, 10, 7, 12, 24, tzinfo=UTC)
        first = manual_run_id(logical, "github-run-1")
        second = manual_run_id(logical, "github-run-2")
        self.assertNotEqual(first, second)
        self.assertIn("_manual_", first)
        self.assertNotEqual(first, scheduled_run_id(datetime(2026, 10, 7, 12, 23, tzinfo=UTC)))
        created = datetime(2026, 10, 7, 12, 24, 19, tzinfo=UTC)
        original = ProductionRunContext.create(
            trigger_type="manual",
            process_started_at_utc=created,
            trigger_created_at_utc=created,
            trigger_id="github-run-1",
        )
        retry = ProductionRunContext.create(
            trigger_type="manual",
            process_started_at_utc=datetime(2026, 10, 7, 12, 30, tzinfo=UTC),
            trigger_created_at_utc=created,
            trigger_id="github-run-1",
        )
        self.assertEqual(original.run_id, retry.run_id)

    def test_acquisition_context_uses_next_minute_without_market_layer_rounding(self):
        self.assertEqual(
            acquisition_minute(self.started), datetime(2026, 10, 7, 12, 25, tzinfo=UTC)
        )
        aligned = datetime(2026, 10, 7, 11, 5, tzinfo=UTC)
        self.assertEqual(acquisition_minute(aligned), aligned)

    def test_structured_run_separates_logical_start_and_acquisition(self):
        pipeline = FakePipeline(StructuredShadow())
        factory_calls = []

        def factory(**kwargs):
            factory_calls.append(kwargs)
            return pipeline

        sleeps = []
        result = run_production_shadow(
            self.context,
            pipeline_factory=factory,
            clock=lambda: self.started,
            sleep=sleeps.append,
        ).to_dict()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["logical_run_at_utc"], "2026-10-07T12:23:00Z")
        self.assertEqual(result["process_started_at_utc"], "2026-10-07T12:24:19Z")
        self.assertEqual(result["acquisition_at_utc"], "2026-10-07T12:25:00Z")
        self.assertEqual(sleeps, [41.0])
        self.assertEqual(factory_calls[0]["run_at_utc"].second, 0)
        self.assertEqual(pipeline.calls[0]["run_id"], self.context.run_id)
        self.assertEqual(
            pipeline.calls[0]["run_identity_at_utc"],
            datetime(2026, 10, 7, 12, 23, tzinfo=UTC),
        )
        self.assertTrue(result["no_order_executed"])

    def test_pipeline_failure_is_structured_by_stage_and_cause(self):
        cause = DataUnavailableError("offline", source="bybit_api")
        failure = ShadowRunError(
            "market snapshot acquisition failed",
            code=ShadowRunErrorCode.MARKET_DATA_FAILED,
            cause=cause,
        )
        pipeline = FakePipeline(failure)
        result = run_production_shadow(
            self.context,
            pipeline_factory=lambda **_: pipeline,
            clock=lambda: datetime(2026, 10, 7, 11, 5, tzinfo=UTC),
        ).to_dict()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["stage"], "MARKET_DATA")
        self.assertEqual(result["failure"]["category"], "SOURCE_UNAVAILABLE")
        self.assertEqual(result["failure"]["source"], "bybit_api")
        self.assertTrue(result["no_order_executed"])

    def test_completed_duplicate_suppresses_success_notification(self):
        pipeline = FakePipeline(ShadowRunAlreadyCompletedError(self.context.run_id))
        result = run_production_shadow(
            self.context,
            pipeline_factory=lambda **_: pipeline,
            clock=lambda: datetime(2026, 10, 7, 11, 5, tzinfo=UTC),
        ).to_dict()
        self.assertEqual(result["status"], "already_completed")
        self.assertEqual(result["notification_status"], "suppressed")
        self.assertTrue(result["duplicate_notification_suppressed"])

    def test_production_modules_have_no_execution_or_ledger_mutation_path(self):
        production = (ROOT / "src/btc_dca_bridge/production.py").read_text()
        telegram = (ROOT / "src/btc_dca_bridge/notifications/telegram.py").read_text()
        for text in (production, telegram):
            self.assertNotIn("place_order", text)
            self.assertNotIn("append_execution", text)
            self.assertNotIn("private", text.lower())


if __name__ == "__main__":
    unittest.main()
