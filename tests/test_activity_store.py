import activity_store
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest import mock

from activity_store import (
    ActivityConfig,
    ActivityStore,
    kst_day_for_epoch,
    kst_range_to_epoch,
)


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

    def test_config_apis_reject_fractional_effective_epochs(self):
        operations = [
            lambda: self.store.apply_config_change(
                1, target_role_id=10, effective_at_epoch=100.5
            ),
            lambda: self.store.invalidate_sod_eod_channel(
                1, effective_at_epoch=100.5
            ),
        ]

        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(TypeError):
                    operation()

        self.assertEqual(
            self.store.get_config(1),
            ActivityConfig(1, None, None, None, None, None),
        )

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


class VoiceSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ActivityStore(str(Path(self.tmp.name) / "activity.db"))
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_kind_transition_and_clip(self):
        self.store.reconcile_session(1, 2, "reading_room", 100)
        self.store.reconcile_session(1, 2, "reading_room", 130)
        self.store.reconcile_session(1, 2, "study", 160)
        self.store.reconcile_session(1, 2, None, 220, close_reason="normal")

        self.assertEqual(
            self.store.list_sessions(1, 2),
            [
                ("reading_room", 100, 160, "category_change"),
                ("study", 160, 220, "normal"),
            ],
        )
        self.assertEqual(
            self.store.voice_seconds_for_range(1, 2, "study", 170, 200), 30
        )

    def test_voice_count_includes_only_positive_overlap_sessions(self):
        self.store.reconcile_session(1, 2, "study", 100)
        self.store.reconcile_session(1, 2, None, 120, close_reason="normal")
        self.store.reconcile_session(1, 2, "study", 120)
        self.store.reconcile_session(1, 2, None, 140, close_reason="normal")

        self.assertEqual(
            self.store.voice_session_count_for_range(1, 2, "study", 120, 130),
            1,
        )

    def test_voice_apis_reject_fractional_epochs(self):
        cases = [
            (
                "reconcile effective_at_epoch",
                lambda store: store.reconcile_session(1, 2, "study", 100.5),
            ),
            (
                "collection run started_epoch",
                lambda store: store.open_collection_run(1, 100.5),
            ),
            (
                "close effective_at_epoch",
                lambda store: store.close_open_rows(
                    1, 100.5, "gateway_disconnect"
                ),
            ),
            (
                "checkpoint_epoch",
                lambda store: store.checkpoint_open_rows(1, 100.5),
            ),
            (
                "seconds range_start",
                lambda store: store.voice_seconds_for_range(
                    1, 2, "study", 0.5, 100
                ),
            ),
            (
                "seconds range_end",
                lambda store: store.voice_seconds_for_range(
                    1, 2, "study", 0, 100.5
                ),
            ),
            (
                "count range_start",
                lambda store: store.voice_session_count_for_range(
                    1, 2, "study", 0.5, 100
                ),
            ),
            (
                "count range_end",
                lambda store: store.voice_session_count_for_range(
                    1, 2, "study", 0, 100.5
                ),
            ),
            (
                "coverage range_start",
                lambda store: store.voice_coverage_for_range(1, 0.5, 100),
            ),
            (
                "coverage range_end",
                lambda store: store.voice_coverage_for_range(1, 0, 100.5),
            ),
        ]

        for label, operation in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                store = ActivityStore(str(Path(tmp) / "activity.db"))
                store.initialize()
                with self.assertRaises(TypeError):
                    operation(store)

    def test_voice_schema_rejects_fractional_epochs(self):
        statements = [
            """
            INSERT INTO voice_sessions(
                guild_id, user_id, activity_kind, started_epoch,
                last_checkpoint_epoch
            ) VALUES (1, 2, 'study', 100.5, 101)
            """,
            """
            INSERT INTO voice_collection_runs(
                guild_id, started_epoch, last_checkpoint_epoch
            ) VALUES (1, 100.5, 101)
            """,
        ]

        for statement in statements:
            with self.subTest(table=statement.split()[2]), closing(
                self.store._connect()
            ) as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(statement)

    def test_disconnect_creates_exact_gap(self):
        self.store.open_collection_run(1, 100)
        self.store.close_open_rows(1, 160, "gateway_disconnect")
        self.store.open_collection_run(1, 200)

        self.assertEqual(
            self.store.voice_coverage_for_range(1, 100, 240).gaps,
            [(160, 200)],
        )

    def test_coverage_merges_overlapping_and_adjacent_runs(self):
        with closing(self.store._connect()) as conn:
            conn.executemany(
                """
                INSERT INTO voice_collection_runs(
                    guild_id, started_epoch, last_checkpoint_epoch,
                    ended_epoch, ended_reason
                ) VALUES (1, ?, ?, ?, 'gateway_disconnect')
                """,
                [(90, 90, 150), (140, 140, 180), (180, 180, 210)],
            )
            conn.commit()

        coverage = self.store.voice_coverage_for_range(1, 100, 200)

        self.assertEqual(coverage.covered, [(100, 200)])
        self.assertEqual(coverage.gaps, [])

    def test_open_rows_use_query_end_not_later_checkpoint(self):
        self.store.reconcile_session(1, 2, "study", 100)
        self.store.open_collection_run(1, 100)
        self.store.checkpoint_open_rows(1, 300)

        self.assertEqual(
            self.store.voice_seconds_for_range(1, 2, "study", 0, 200), 100
        )
        self.assertEqual(
            self.store.voice_session_count_for_range(1, 2, "study", 0, 200), 1
        )
        self.assertEqual(
            self.store.voice_coverage_for_range(1, 0, 200).covered,
            [(100, 200)],
        )

    def test_open_session_unique_race_rechecks_after_injected_duplicate(self):
        with closing(self.store._connect()) as conn:
            conn.execute(
                """
                INSERT INTO voice_sessions(
                    guild_id, user_id, activity_kind, started_epoch,
                    last_checkpoint_epoch
                ) VALUES (1, 2, 'study', 90, 90)
                """
            )
            conn.commit()
        original_get = self.store._get_open_session_in_tx
        unique = sqlite3.IntegrityError("UNIQUE constraint failed")
        unique.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE
        unique.sqlite_errorname = "SQLITE_CONSTRAINT_UNIQUE"
        calls = iter([None])

        def stale_then_actual(conn, guild_id, user_id):
            try:
                return next(calls)
            except StopIteration:
                return original_get(conn, guild_id, user_id)

        with mock.patch.object(
            self.store, "_get_open_session_in_tx", side_effect=stale_then_actual
        ), mock.patch.object(
            self.store, "_insert_open_session_in_tx", side_effect=unique
        ):
            self.store.reconcile_session(1, 2, "study", 100)

        self.assertEqual(self.store.open_session_count(1, 2), 1)
        self.assertEqual(
            self.store.list_sessions(1, 2),
            [("study", 90, None, None)],
        )

    def test_unique_race_different_kind_transitions_and_check_error_reraises(self):
        with closing(self.store._connect()) as conn:
            conn.execute(
                """
                INSERT INTO voice_sessions(
                    guild_id, user_id, activity_kind, started_epoch,
                    last_checkpoint_epoch
                ) VALUES (1, 2, 'study', 90, 90)
                """
            )
            conn.commit()
        original_get = self.store._get_open_session_in_tx
        original_insert = self.store._insert_open_session_in_tx
        unique = sqlite3.IntegrityError("UNIQUE constraint failed")
        unique.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE
        unique.sqlite_errorname = "SQLITE_CONSTRAINT_UNIQUE"
        reads = iter([None])
        inserts = iter([unique, None])

        def stale_then_actual(conn, guild_id, user_id):
            try:
                return next(reads)
            except StopIteration:
                return original_get(conn, guild_id, user_id)

        def unique_then_real(*args):
            outcome = next(inserts)
            if outcome is not None:
                raise outcome
            return original_insert(*args)

        with mock.patch.object(
            self.store, "_get_open_session_in_tx", side_effect=stale_then_actual
        ), mock.patch.object(
            self.store,
            "_insert_open_session_in_tx",
            side_effect=unique_then_real,
        ):
            self.store.reconcile_session(1, 2, "reading_room", 100)

        self.assertEqual(
            self.store.list_sessions(1, 2),
            [
                ("study", 90, 100, "category_change"),
                ("reading_room", 100, None, None),
            ],
        )
        bad = sqlite3.IntegrityError("CHECK constraint failed")
        bad.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_CHECK
        with mock.patch.object(
            self.store, "_insert_open_session_in_tx", side_effect=bad
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.reconcile_session(1, 3, "study", 100)

    def test_graceful_shutdown_uses_session_and_run_reason_sets(self):
        self.store.reconcile_session(1, 2, "study", 100)
        self.store.open_collection_run(1, 100)

        self.store.close_open_rows(1, 120, "graceful_shutdown")

        self.assertEqual(self.store.list_sessions(1, 2)[0][3], "reconciled")
        self.assertEqual(self.store.list_runs(1)[0][3], "graceful_shutdown")


class SodEodStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ActivityStore(str(Path(self.tmp.name) / "activity.db"))
        self.store.initialize()
        self.store.apply_config_change(
            1, sod_eod_channel_id=2, effective_at_epoch=99
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _event_count(self):
        with closing(self.store._connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM sod_eod_events").fetchone()[0]

    def test_event_daily_cursor_are_atomic_and_types_are_validated(self):
        with self.assertRaises(ValueError):
            self.store.record_backfill_message_and_advance_cursor(
                guild_id=1,
                channel_id=2,
                message_id=3,
                user_id=4,
                message_created_epoch=100,
                event_types={"bad"},
                newest_processed_message_created_epoch=100,
                updated_epoch=101,
                expected_current_channel_id=2,
            )

        self.assertIsNone(
            self.store.get_sync_state(1, 2).newest_processed_message_id
        )
        with closing(self.store._connect()) as conn:
            conn.execute(
                """
                CREATE TRIGGER fail_daily BEFORE INSERT ON sod_eod_daily
                BEGIN SELECT RAISE(ABORT, 'daily failed'); END
                """
            )
            conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_backfill_message_and_advance_cursor(
                guild_id=1,
                channel_id=2,
                message_id=3,
                user_id=4,
                message_created_epoch=100,
                event_types={"sod"},
                newest_processed_message_created_epoch=100,
                updated_epoch=101,
                expected_current_channel_id=2,
            )

        self.assertEqual(self._event_count(), 0)
        self.assertEqual(self.store.daily_types(1, 4, "1970-01-01"), set())
        self.assertIsNone(
            self.store.get_sync_state(1, 2).newest_processed_message_id
        )
        with closing(self.store._connect()) as conn:
            conn.execute("DROP TRIGGER fail_daily")
            conn.commit()

        self.store.record_backfill_message_and_advance_cursor(
            guild_id=1,
            channel_id=2,
            message_id=3,
            user_id=4,
            message_created_epoch=100,
            event_types={"sod", "eod"},
            newest_processed_message_created_epoch=100,
            updated_epoch=101,
            expected_current_channel_id=2,
        )

        self.assertEqual(
            self.store.daily_types(1, 4, "1970-01-01"), {"sod", "eod"}
        )
        self.assertEqual(self._event_count(), 2)

    def test_same_message_live_then_backfill_is_idempotent(self):
        self.store.record_live_message(
            guild_id=1,
            channel_id=2,
            message_id=10,
            user_id=4,
            message_created_epoch=100,
            event_types={"sod", "eod"},
            updated_epoch=101,
            expected_current_channel_id=2,
        )

        state = self.store.get_sync_state(1, 2)
        self.assertIsNone(state.newest_processed_message_id)
        self.assertIsNone(state.history_from_epoch)

        self.store.record_backfill_message_and_advance_cursor(
            guild_id=1,
            channel_id=2,
            message_id=10,
            user_id=4,
            message_created_epoch=100,
            event_types={"sod", "eod"},
            newest_processed_message_created_epoch=100,
            updated_epoch=102,
            expected_current_channel_id=2,
        )

        state = self.store.get_sync_state(1, 2)
        self.assertEqual(
            (
                state.newest_processed_message_created_epoch,
                state.newest_processed_message_id,
            ),
            (100, 10),
        )
        self.assertEqual(
            self.store.daily_types(1, 4, "1970-01-01"), {"sod", "eod"}
        )
        self.assertEqual(self._event_count(), 2)

    def test_partial_resume_and_delta_scans_preserve_history_and_completion(self):
        self.store.mark_backfill_started(1, 2, 100)
        self.store.record_backfill_message_and_advance_cursor(
            guild_id=1,
            channel_id=2,
            message_id=20,
            user_id=4,
            message_created_epoch=50,
            event_types=set(),
            newest_processed_message_created_epoch=50,
            updated_epoch=101,
            expected_current_channel_id=2,
        )
        partial = self.store.get_sync_state(1, 2)
        self.assertEqual(partial.newest_processed_message_id, 20)
        self.assertEqual(partial.newest_processed_message_created_epoch, 50)
        self.assertEqual(partial.history_from_epoch, 50)
        self.assertIsNone(partial.completed_epoch)

        self.store.mark_backfill_started(1, 2, 110)
        self.store.record_backfill_message_and_advance_cursor(
            guild_id=1,
            channel_id=2,
            message_id=21,
            user_id=5,
            message_created_epoch=60,
            event_types=set(),
            newest_processed_message_created_epoch=60,
            updated_epoch=111,
            expected_current_channel_id=2,
        )
        self.store.mark_backfill_completed(1, 2, 120)
        resumed = self.store.get_sync_state(1, 2)
        self.assertEqual(resumed.newest_processed_message_id, 21)
        self.assertEqual(resumed.newest_processed_message_created_epoch, 60)
        self.assertEqual(resumed.history_from_epoch, 50)
        self.assertEqual(resumed.completed_epoch, 120)

        self.store.mark_backfill_started(1, 2, 130)
        started_delta = self.store.get_sync_state(1, 2)
        self.assertIsNone(started_delta.completed_epoch)
        self.store.record_backfill_message_and_advance_cursor(
            guild_id=1,
            channel_id=2,
            message_id=22,
            user_id=6,
            message_created_epoch=140,
            event_types=set(),
            newest_processed_message_created_epoch=140,
            updated_epoch=141,
            expected_current_channel_id=2,
        )
        self.store.mark_backfill_completed(1, 2, 150)
        delta = self.store.get_sync_state(1, 2)
        self.assertEqual(delta.newest_processed_message_id, 22)
        self.assertEqual(delta.newest_processed_message_created_epoch, 140)
        self.assertEqual(delta.history_from_epoch, 50)
        self.assertEqual(delta.completed_epoch, 150)

    def test_backfill_cursor_never_regresses_lexicographic_marker(self):
        self.store.record_backfill_message_and_advance_cursor(
            guild_id=1,
            channel_id=2,
            message_id=20,
            user_id=4,
            message_created_epoch=100,
            event_types=set(),
            newest_processed_message_created_epoch=100,
            updated_epoch=200,
            expected_current_channel_id=2,
        )
        self.store.record_backfill_message_and_advance_cursor(
            guild_id=1,
            channel_id=2,
            message_id=99,
            user_id=4,
            message_created_epoch=90,
            event_types=set(),
            newest_processed_message_created_epoch=90,
            updated_epoch=190,
            expected_current_channel_id=2,
        )

        state = self.store.get_sync_state(1, 2)
        self.assertEqual(
            (
                state.newest_processed_message_created_epoch,
                state.newest_processed_message_id,
            ),
            (100, 20),
        )
        self.assertEqual(state.history_from_epoch, 90)

        self.store.record_backfill_message_and_advance_cursor(
            guild_id=1,
            channel_id=2,
            message_id=19,
            user_id=4,
            message_created_epoch=100,
            event_types=set(),
            newest_processed_message_created_epoch=100,
            updated_epoch=200,
            expected_current_channel_id=2,
        )
        equal_epoch_lower_id = self.store.get_sync_state(1, 2)
        self.assertEqual(
            (
                equal_epoch_lower_id.newest_processed_message_created_epoch,
                equal_epoch_lower_id.newest_processed_message_id,
            ),
            (100, 20),
        )
        self.assertEqual(equal_epoch_lower_id.history_from_epoch, 90)

        self.store.record_backfill_message_and_advance_cursor(
            guild_id=1,
            channel_id=2,
            message_id=21,
            user_id=4,
            message_created_epoch=100,
            event_types=set(),
            newest_processed_message_created_epoch=100,
            updated_epoch=201,
            expected_current_channel_id=2,
        )
        advanced = self.store.get_sync_state(1, 2)
        self.assertEqual(
            (
                advanced.newest_processed_message_created_epoch,
                advanced.newest_processed_message_id,
            ),
            (100, 21),
        )
        self.assertEqual(advanced.history_from_epoch, 90)

    def test_event_and_sync_schema_reject_fractional_epochs(self):
        statements = [
            """
            INSERT INTO sod_eod_events(
                message_id, guild_id, user_id, event_date_kst, event_type,
                message_created_epoch, channel_id
            ) VALUES (50, 1, 4, '1970-01-01', 'sod', 100.5, 2)
            """,
            """
            UPDATE activity_sync_state
            SET newest_processed_message_created_epoch=100.5
            WHERE guild_id=1 AND channel_id=2
            """,
            """
            UPDATE activity_sync_state SET history_from_epoch=100.5
            WHERE guild_id=1 AND channel_id=2
            """,
            """
            UPDATE activity_sync_state SET completed_epoch=100.5
            WHERE guild_id=1 AND channel_id=2
            """,
            """
            UPDATE activity_sync_state SET updated_epoch=100.5
            WHERE guild_id=1 AND channel_id=2
            """,
        ]

        for statement in statements:
            with self.subTest(statement=statement), closing(
                self.store._connect()
            ) as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(statement)

    def test_channel_change_rejects_live_and_backfill_with_zero_writes(self):
        self.store.apply_config_change(
            1, sod_eod_channel_id=3, effective_at_epoch=102
        )

        for operation in (
            lambda: self.store.record_live_message(
                guild_id=1,
                channel_id=2,
                message_id=30,
                user_id=4,
                message_created_epoch=100,
                event_types={"sod"},
                updated_epoch=103,
                expected_current_channel_id=2,
            ),
            lambda: self.store.record_backfill_message_and_advance_cursor(
                guild_id=1,
                channel_id=2,
                message_id=31,
                user_id=4,
                message_created_epoch=101,
                event_types={"eod"},
                newest_processed_message_created_epoch=101,
                updated_epoch=103,
                expected_current_channel_id=2,
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(activity_store.ChannelChanged):
                    operation()

        self.assertEqual(self._event_count(), 0)
        self.assertEqual(self.store.daily_types(1, 4, "1970-01-01"), set())
        self.assertIsNone(
            self.store.get_sync_state(1, 2).newest_processed_message_id
        )

    def test_event_and_sync_apis_reject_fractional_epochs(self):
        operations = [
            lambda: self.store.record_live_message(
                guild_id=1,
                channel_id=2,
                message_id=40,
                user_id=4,
                message_created_epoch=100.5,
                event_types={"sod"},
                updated_epoch=101,
                expected_current_channel_id=2,
            ),
            lambda: self.store.record_backfill_message_and_advance_cursor(
                guild_id=1,
                channel_id=2,
                message_id=41,
                user_id=4,
                message_created_epoch=100,
                event_types=set(),
                newest_processed_message_created_epoch=100.5,
                updated_epoch=101,
                expected_current_channel_id=2,
            ),
            lambda: self.store.mark_backfill_started(1, 2, 100.5),
            lambda: self.store.mark_backfill_completed(1, 2, 100.5),
        ]

        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(TypeError):
                operation()

        self.assertEqual(self._event_count(), 0)
        self.assertIsNone(
            self.store.get_sync_state(1, 2).newest_processed_message_id
        )
