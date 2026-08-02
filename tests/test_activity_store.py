import tempfile
import unittest
from contextlib import closing
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

    def _insert_open_voice_rows(self):
        with closing(self.store._connect()) as conn:
            conn.execute(
                """
                INSERT INTO voice_sessions(
                    guild_id, user_id, activity_kind, started_epoch, last_checkpoint_epoch
                ) VALUES (1, 2, 'reading_room', 100, 100)
                """
            )
            conn.execute(
                """
                INSERT INTO voice_collection_runs(guild_id, started_epoch, last_checkpoint_epoch)
                VALUES (1, 100, 100)
                """
            )
            conn.commit()

    def _open_voice_rows(self):
        with closing(self.store._connect()) as conn:
            return (
                conn.execute(
                    "SELECT ended_epoch, closed_reason FROM voice_sessions WHERE guild_id=1"
                ).fetchall(),
                conn.execute(
                    "SELECT ended_epoch, ended_reason FROM voice_collection_runs WHERE guild_id=1"
                ).fetchall(),
            )

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
        with closing(self.store._connect()) as conn:
            conn.execute(
                """
                UPDATE activity_sync_state
                SET newest_processed_message_id=700,
                    newest_processed_message_created_epoch=90,
                    history_from_epoch=80,
                    completed_epoch=250
                WHERE guild_id=1 AND channel_id=40
                """
            )
            conn.commit()
        self.store.apply_config_change(1, sod_eod_channel_id=41, effective_at_epoch=200)
        self.store.apply_config_change(1, sod_eod_channel_id=40, effective_at_epoch=300)

        self.assertEqual(
            self.store.list_channel_periods(1),
            [(40, 100, 200), (41, 200, 300), (40, 300, None)],
        )
        with closing(self.store._connect()) as conn:
            state = conn.execute(
                """
                SELECT newest_processed_message_id, newest_processed_message_created_epoch,
                       history_from_epoch, completed_epoch, updated_epoch
                FROM activity_sync_state
                WHERE guild_id=1 AND channel_id=40
                """
            ).fetchone()
        self.assertEqual(state, (700, 90, 80, 250, 100))

    def test_collection_started_epoch_is_written_once(self):
        self.store.apply_config_change(1, target_role_id=10, effective_at_epoch=100)
        self.store.apply_config_change(1, reading_category_id=20, effective_at_epoch=110)
        first = self.store.apply_config_change(1, study_category_id=30, effective_at_epoch=120)
        later = self.store.apply_config_change(1, reading_category_id=21, effective_at_epoch=130)

        self.assertEqual(first.voice_collection_started_epoch, 120)
        self.assertEqual(later.voice_collection_started_epoch, 120)

    def test_invalid_config_preserves_prior_open_state(self):
        self.store.apply_config_change(
            1,
            target_role_id=10,
            reading_category_id=20,
            study_category_id=30,
            effective_at_epoch=100,
        )
        self._insert_open_voice_rows()

        with self.assertRaises(ValueError):
            self.store.apply_config_change(
                1,
                reading_category_id=31,
                study_category_id=31,
                effective_at_epoch=120,
            )

        config = self.store.get_config(1)
        self.assertEqual((config.reading_category_id, config.study_category_id), (20, 30))
        self.assertEqual(self._open_voice_rows(), ([(None, None)], [(None, None)]))

    def test_voice_core_change_closes_open_rows_at_effective_epoch(self):
        self.store.apply_config_change(
            1,
            target_role_id=10,
            reading_category_id=20,
            study_category_id=30,
            effective_at_epoch=100,
        )
        self._insert_open_voice_rows()

        self.store.apply_config_change(1, reading_category_id=21, effective_at_epoch=120)

        self.assertEqual(
            self._open_voice_rows(),
            ([(120, "config_changed")], [(120, "config_changed")]),
        )

    def test_sod_only_change_preserves_open_voice_rows(self):
        self.store.apply_config_change(
            1,
            target_role_id=10,
            reading_category_id=20,
            study_category_id=30,
            effective_at_epoch=100,
        )
        self._insert_open_voice_rows()

        self.store.apply_config_change(1, sod_eod_channel_id=40, effective_at_epoch=120)

        self.assertEqual(self._open_voice_rows(), ([(None, None)], [(None, None)]))

    def test_invalidated_sod_channel_closes_period_as_config_invalid(self):
        self.store.apply_config_change(1, sod_eod_channel_id=40, effective_at_epoch=100)

        self.store.invalidate_sod_eod_channel(1, effective_at_epoch=120)

        self.assertIsNone(self.store.get_config(1).sod_eod_channel_id)
        with closing(self.store._connect()) as conn:
            period = conn.execute(
                """
                SELECT channel_id, started_epoch, ended_epoch, ended_reason
                FROM sod_eod_channel_periods
                WHERE guild_id=1
                """
            ).fetchone()
        self.assertEqual(period, (40, 100, 120, "config_invalid"))
