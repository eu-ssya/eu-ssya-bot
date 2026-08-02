import tempfile
import unittest
from datetime import date
from pathlib import Path

from activity_store import ActivityStore, kst_day_for_epoch, kst_range_to_epoch


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ActivityStore(str(Path(self.tmp.name) / "activity.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_is_idempotent(self):
        self.store.initialize()
        self.store.initialize()
        self.assertEqual(
            set(self.store.table_names()),
            {
                "activity_config",
                "voice_sessions",
                "voice_collection_runs",
                "sod_eod_events",
                "sod_eod_daily",
                "activity_sync_state",
                "sod_eod_channel_periods",
            },
        )

    def test_kst_day_range_includes_end_date(self):
        start, end = kst_range_to_epoch(date(2026, 8, 2), date(2026, 8, 2))
        self.assertEqual(end - start, 86400)
        self.assertEqual(kst_day_for_epoch(start), "2026-08-02")


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ActivityStore(str(Path(self.tmp.name) / "activity.db"))
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_partial_config_and_invalid_rollback(self):
        self.store.apply_config_change(1, target_role_id=10, effective_at_epoch=100)

        with self.assertRaises(ValueError):
            self.store.apply_config_change(
                1,
                reading_category_id=20,
                study_category_id=20,
                effective_at_epoch=110,
            )

        config = self.store.get_config(1)
        self.assertEqual(config.target_role_id, 10)
        self.assertIsNone(config.reading_category_id)

    def test_channel_period_a_b_a(self):
        self.store.apply_config_change(1, sod_eod_channel_id=40, effective_at_epoch=100)
        self.store.apply_config_change(1, sod_eod_channel_id=41, effective_at_epoch=200)
        self.store.apply_config_change(1, sod_eod_channel_id=40, effective_at_epoch=300)

        self.assertEqual(
            self.store.list_channel_periods(1),
            [(40, 100, 200), (41, 200, 300), (40, 300, None)],
        )

    def test_collection_started_epoch_is_written_once(self):
        self.store.apply_config_change(1, target_role_id=10, effective_at_epoch=100)
        self.store.apply_config_change(1, reading_category_id=20, effective_at_epoch=110)
        first = self.store.apply_config_change(1, study_category_id=30, effective_at_epoch=120)
        later = self.store.apply_config_change(1, reading_category_id=21, effective_at_epoch=130)

        self.assertEqual(first.voice_collection_started_epoch, 120)
        self.assertEqual(later.voice_collection_started_epoch, 120)
