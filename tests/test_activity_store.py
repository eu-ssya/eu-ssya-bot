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
