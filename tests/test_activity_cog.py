import asyncio
import datetime
import itertools
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord

import bot as bot_module
from activity_store import (
    ActivityStore,
    ChannelChanged,
    CoverageWarning,
    KST,
    ReportRow,
    kst_range_to_epoch,
)

from tests.activity_fixtures import (
    FakeBot,
    FakeGuild,
    FakeMember,
    configured_fixture,
    coverage_gaps,
    fake_category,
    fake_deferred_interaction,
    fake_interaction,
    fake_role,
    fake_text_channel,
    checkpoint_once_for_test,
    make_controlled_history_channel,
    make_history_channel,
    on_disconnect_for_test,
    on_resumed_for_test,
    make_report,
    prepare_sync_marker,
    recover_after_ready_for_test,
    record_live_message_for_test,
    run_checkpoint,
    press,
    report_with_members,
    session_rows,
    set_sod_channel_for_test,
    sync_state,
)


class FixtureContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_followup_send_records_content_files_and_ephemeral_kwargs(self):
        interaction = fake_interaction(user_id=1, administrator=True)
        files = [object()]

        self.assertTrue(hasattr(interaction.followup, "send"))
        await interaction.followup.send(
            "report ready",
            files=files,
            ephemeral=True,
        )

        self.assertEqual(
            interaction.followup.sent,
            [("report ready", {"files": files, "ephemeral": True})],
        )

    def test_fake_member_has_default_display_name_and_preserves_custom_name(self):
        self.assertIn("display_name", FakeMember.__dataclass_fields__)
        default_member = FakeMember(1, object(), {10})
        named_member = FakeMember(2, object(), {10}, display_name="Alice")

        self.assertTrue(default_member.display_name)
        self.assertEqual(named_member.display_name, "Alice")
        self.assertEqual(named_member.in_category(20).display_name, "Alice")
        self.assertEqual(named_member.with_roles({20}).display_name, "Alice")


class LoadIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_failure_does_not_skip_wallet_or_rss(self):
        bot = FakeBot(fail_extension="activity_cog")

        self.assertTrue(hasattr(bot_module, "_load_extensions"))
        await bot_module._load_extensions(bot, bot.start_rss)

        self.assertEqual(
            bot.calls,
            ["activity_cog", "wallet_cog", "rss_loop.start"],
        )

    async def test_successful_load_keeps_activity_wallet_rss_order(self):
        bot = FakeBot()

        self.assertTrue(hasattr(bot_module, "_load_extensions"))
        await bot_module._load_extensions(bot, bot.start_rss)

        self.assertEqual(
            bot.calls,
            ["activity_cog", "wallet_cog", "rss_loop.start"],
        )

    def test_activity_required_intents_are_enabled(self):
        self.assertTrue(bot_module.bot.intents.members)
        self.assertTrue(bot_module.bot.intents.message_content)
        self.assertTrue(bot_module.bot.intents.voice_states)


class ActivitySetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_initializes_store_off_loop_before_adding_cog(self):
        from activity_cog import ActivityCog, setup

        event_loop_thread = threading.get_ident()
        events = []

        class RecordingStore:
            def __init__(self, db_path):
                self.db_path = db_path

            def initialize(self):
                events.append(("initialize", threading.get_ident()))

        class RecordingBot:
            def __init__(self):
                self.added_cogs = []

            async def add_cog(self, cog):
                events.append(("add_cog", threading.get_ident()))
                self.added_cogs.append(cog)

        target_bot = RecordingBot()
        with mock.patch.dict(
            os.environ,
            {"ACTIVITY_DB_PATH": "test-activity.db"},
        ), mock.patch("activity_cog.ActivityStore", RecordingStore):
            await setup(target_bot)

        self.assertEqual(
            [name for name, _thread_id in events],
            ["initialize", "add_cog"],
        )
        self.assertNotEqual(events[0][1], event_loop_thread)
        self.assertEqual(events[1][1], event_loop_thread)
        self.assertEqual(len(target_bot.added_cogs), 1)
        self.assertIsInstance(target_bot.added_cogs[0], ActivityCog)
        self.assertEqual(target_bot.added_cogs[0].store.db_path, "test-activity.db")

    async def test_setup_uses_data_volume_path_by_default(self):
        from activity_cog import setup

        paths = []

        class RecordingStore:
            def __init__(self, db_path):
                paths.append(db_path)

            def initialize(self):
                return None

        target_bot = mock.AsyncMock()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "activity_cog.ActivityStore",
            RecordingStore,
        ):
            await setup(target_bot)

        self.assertEqual(paths, ["/data/activity.db"])
        target_bot.add_cog.assert_awaited_once()


class StoreBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from activity_cog import ActivityCog

        self.cog = ActivityCog(mock.Mock(), mock.Mock())

    async def test_store_call_preserves_positional_and_keyword_arguments(self):
        calls = []

        def recording_method(*args, **kwargs):
            calls.append((args, kwargs))
            return "stored"

        result = await self.cog._store_call(
            recording_method,
            1,
            "two",
            guild_id=3,
            effective_at_epoch=4,
        )

        self.assertEqual(result, "stored")
        self.assertEqual(
            calls,
            [((1, "two"), {"guild_id": 3, "effective_at_epoch": 4})],
        )

    async def test_store_call_rethrows_original_database_exception(self):
        error = sqlite3.Error("database unavailable")

        def failing_method():
            raise error

        with self.assertRaises(sqlite3.Error) as raised:
            await self.cog._store_call(failing_method)

        self.assertIs(raised.exception, error)

    async def test_store_call_serializes_whole_synchronous_calls(self):
        boundary_entries = []
        method_calls = []
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()

        def recording_method(name):
            method_calls.append(name)
            return name

        async def controlled_to_thread(method, *args, **kwargs):
            name = args[0]
            boundary_entries.append(name)
            if name == "first":
                first_entered.set()
                await release_first.wait()
            return method(*args, **kwargs)

        async def invoke_second():
            second_started.set()
            return await self.cog._store_call(recording_method, "second")

        with mock.patch("activity_cog.asyncio.to_thread", controlled_to_thread):
            first = asyncio.create_task(
                self.cog._store_call(recording_method, "first")
            )
            await first_entered.wait()
            second = asyncio.create_task(invoke_second())
            await second_started.wait()

            self.assertEqual(boundary_entries, ["first"])
            self.assertEqual(method_calls, [])

            release_first.set()
            self.assertEqual(
                await asyncio.gather(first, second),
                ["first", "second"],
            )

        self.assertEqual(boundary_entries, ["first", "second"])
        self.assertEqual(method_calls, ["first", "second"])

    async def test_cancelled_store_call_holds_lock_until_worker_finishes(self):
        worker_entered = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()

        def slow_method():
            worker_entered.set()
            release_worker.wait(5)
            worker_finished.set()
            return "stored"

        call = asyncio.create_task(self.cog._store_call(slow_method))
        while not worker_entered.is_set():
            await asyncio.sleep(0)
        call.cancel()
        await asyncio.sleep(0)
        cancellation_waited = not call.done() and self.cog.store_lock.locked()
        release_worker.set()
        with self.assertRaises(asyncio.CancelledError):
            await call

        self.assertTrue(cancellation_waited)
        self.assertTrue(worker_finished.is_set())
        self.assertFalse(self.cog.store_lock.locked())
        self.assertEqual(self.cog._store_worker_tasks, set())

    def test_cog_has_shared_store_lock_and_per_guild_locks_and_gates(self):
        self.assertIs(self.cog.guild_locks[1], self.cog.guild_locks[1])
        self.assertIsNot(self.cog.guild_locks[1], self.cog.guild_locks[2])
        self.assertIs(self.cog.collection_gates[1], self.cog.collection_gates[1])
        self.assertIsNot(self.cog.collection_gates[1], self.cog.collection_gates[2])
        self.assertFalse(self.cog.collection_gates[1].is_set())


class VoiceListenerTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog, guild, member = configured_fixture()
        self.addCleanup(cog._test_tmp.cleanup)
        return cog, guild, member

    async def call_at(self, epoch, callback, *args):
        with mock.patch("activity_cog.utc_now_epoch", return_value=epoch):
            await callback(*args)

    async def test_role_and_kind_transitions_use_specific_close_reasons(self):
        cog, _guild, member = self.make_cog()

        await cog.reconcile_member(member.in_category(20), 100)
        await cog.reconcile_member(member.in_category(30), 150)
        await cog.reconcile_member(
            member.in_category(30).with_roles(set()),
            200,
            close_reason="role_removed",
        )

        self.assertEqual(
            await session_rows(cog, member.id),
            [
                ("reading_room", 100, 150, "category_change"),
                ("study", 150, 200, "role_removed"),
            ],
        )

    async def test_desired_kind_filters_bot_role_and_unconfigured_category(self):
        cog, _guild, member = self.make_cog()
        config = await cog._store_call(cog.store.get_config, 1)

        self.assertEqual(cog.desired_kind_for_member(member, config), "reading_room")
        self.assertEqual(
            cog.desired_kind_for_member(member.in_category(30), config), "study"
        )
        self.assertIsNone(
            cog.desired_kind_for_member(member.with_roles(set()), config)
        )
        self.assertIsNone(
            cog.desired_kind_for_member(
                FakeMember(member.id, member.guild, {10}, 20, bot=True), config
            )
        )
        self.assertIsNone(
            cog.desired_kind_for_member(member.in_category(999), config)
        )

    async def test_closed_gate_marks_guild_dirty_without_writing_session(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 100)
        cog.collection_gates[guild.id].clear()

        await cog.reconcile_member(member.in_category(30), 150)

        self.assertIn(guild.id, cog.dirty_guilds)
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, None, None)],
        )

    async def test_voice_updates_reconcile_movement_and_same_kind_is_idempotent(self):
        cog, _guild, member = self.make_cog()
        same_kind_member = member.in_category(20)

        await self.call_at(
            100,
            cog.on_voice_state_update,
            same_kind_member,
            object(),
            object(),
        )
        await self.call_at(
            120,
            cog.on_voice_state_update,
            same_kind_member,
            object(),
            object(),
        )
        await self.call_at(
            150,
            cog.on_voice_state_update,
            member.in_category(30),
            object(),
            object(),
        )

        self.assertEqual(
            await session_rows(cog, member.id),
            [
                ("reading_room", 100, 150, "category_change"),
                ("study", 150, None, None),
            ],
        )

    async def test_member_update_reconciles_only_target_role_membership_diff(self):
        cog, guild, member = self.make_cog()
        before = member.in_category(20)
        irrelevant_after = FakeMember(member.id, guild, {10, 99}, 20)

        with mock.patch.object(
            cog, "reconcile_member", wraps=cog.reconcile_member
        ) as reconcile, mock.patch.object(
            cog,
            "_revalidate_configured_resources_locked",
            wraps=cog._revalidate_configured_resources_locked,
        ) as revalidate:
            await self.call_at(90, cog.on_member_update, before, irrelevant_after)
            reconcile.assert_not_awaited()
            revalidate.assert_not_awaited()

        await cog.reconcile_member(before, 100)
        await self.call_at(
            150,
            cog.on_member_update,
            before,
            before.with_roles(set()),
        )

        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 150, "role_removed")],
        )

    async def test_member_update_target_role_addition_opens_current_voice_session(self):
        cog, _guild, member = self.make_cog()
        before = member.in_category(20).with_roles(set())
        after = member.in_category(20).with_roles({10})

        await self.call_at(150, cog.on_member_update, before, after)

        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 150, None, None)],
        )

    async def test_guild_available_recovers_current_voice_state(self):
        cog, guild, _member = self.make_cog()
        cog.collection_gates[guild.id].clear()

        await self.call_at(100, cog.on_guild_available, guild)

        self.assertTrue(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, guild.id, 1),
            [("reading_room", 100, None, None)],
        )

    async def test_full_reconcile_closes_absent_stored_member_and_keeps_current(self):
        cog, guild, member = self.make_cog()
        await cog._store_call(
            cog.store.reconcile_session,
            guild.id,
            99,
            "study",
            50,
        )

        await cog.full_reconcile_guild(guild, 100)

        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, guild.id, 99),
            [("study", 50, 100, "reconciled")],
        )
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, None, None)],
        )

    async def test_guild_available_closes_absent_stored_member_and_reconciles_current(self):
        cog, guild, member = self.make_cog()
        await cog._store_call(
            cog.store.reconcile_session,
            guild.id,
            99,
            "reading_room",
            50,
        )

        await self.call_at(100, cog.on_guild_available, guild)

        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, guild.id, 99),
            [("reading_room", 50, 50, "restart_checkpoint")],
        )
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, None, None)],
        )

    async def test_role_deletion_uses_passed_role_and_invalidates_voice(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 100)
        deleted = guild.roles[0]
        guild.roles = []

        await self.call_at(150, cog.on_guild_role_delete, deleted)

        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertIsNone(config.target_role_id)
        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 150, "config_changed")],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [(1, 1, 150, "config_invalid")],
        )
        await self.call_at(
            160,
            cog.on_voice_state_update,
            member,
            object(),
            object(),
        )
        self.assertIn(guild.id, cog.dirty_guilds)
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 150, "config_changed")],
        )

    async def test_non_target_role_deletion_revalidates_voice_access_loss(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 100)
        deleted = fake_role(50, guild)
        guild.get_channel(20).permissions_for.return_value = mock.Mock(
            view_channel=False
        )

        await self.call_at(150, cog.on_guild_role_delete, deleted)

        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertIsNone(config.reading_category_id)
        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertNotIn(guild.id, cog.dirty_guilds)
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 150, "config_changed")],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [(1, 1, 150, "config_invalid")],
        )

    async def test_non_target_role_deletion_sod_access_loss_preserves_voice(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 100)
        sessions_before = await cog._store_call(cog.store.list_sessions, 1, 1)
        runs_before = await cog._store_call(cog.store.list_runs, 1)
        count_before = await cog._store_call(
            cog.store.voice_session_count_for_range,
            1,
            1,
            "reading_room",
            0,
            200,
        )
        deleted = fake_role(50, guild)
        guild.get_channel(40).permissions_for.return_value = mock.Mock(
            view_channel=True,
            read_message_history=False,
        )

        await self.call_at(150, cog.on_guild_role_delete, deleted)

        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertIsNone(config.sod_eod_channel_id)
        self.assertTrue(cog.collection_gates[guild.id].is_set())
        self.assertNotIn(guild.id, cog.dirty_guilds)
        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, 1, 1), sessions_before
        )
        self.assertEqual(await cog._store_call(cog.store.list_runs, 1), runs_before)
        self.assertEqual(
            await cog._store_call(
                cog.store.voice_session_count_for_range,
                1,
                1,
                "reading_room",
                0,
                200,
            ),
            count_before,
        )

    async def test_reading_category_deletion_invalidates_voice(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 100)
        deleted = guild.get_channel(20)
        guild.channels.remove(deleted)

        await self.call_at(150, cog.on_guild_channel_delete, deleted)

        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertIsNone(config.reading_category_id)
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 150, "config_changed")],
        )

    async def test_study_category_deletion_invalidates_voice(self):
        cog, guild, member = self.make_cog()
        studying = member.in_category(30)
        await cog.reconcile_member(studying, 100)
        deleted = guild.get_channel(30)
        guild.channels.remove(deleted)

        await self.call_at(150, cog.on_guild_channel_delete, deleted)

        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertIsNone(config.study_category_id)
        self.assertEqual(
            await session_rows(cog, member.id),
            [("study", 100, 150, "config_changed")],
        )

    async def test_sod_channel_deletion_closes_only_period(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 100)
        sessions_before = await cog._store_call(cog.store.list_sessions, 1, 1)
        runs_before = await cog._store_call(cog.store.list_runs, 1)
        deleted = guild.get_channel(40)
        guild.channels.remove(deleted)

        await self.call_at(150, cog.on_guild_channel_delete, deleted)

        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertIsNone(config.sod_eod_channel_id)
        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, 1, 1), sessions_before
        )
        self.assertEqual(await cog._store_call(cog.store.list_runs, 1), runs_before)
        self.assertEqual(
            await cog._store_call(cog.store.list_channel_periods, 1),
            [(40, 1, 150)],
        )

    async def test_category_type_change_invalidates_using_after_object(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 100)
        before = guild.get_channel(20)
        after = fake_text_channel(20, guild)
        guild.channels[guild.channels.index(before)] = after

        await self.call_at(150, cog.on_guild_channel_update, before, after)

        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertIsNone(config.reading_category_id)
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 150, "config_changed")],
        )

    async def test_text_channel_access_loss_preserves_voice_data(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 100)
        sessions_before = await cog._store_call(cog.store.list_sessions, 1, 1)
        runs_before = await cog._store_call(cog.store.list_runs, 1)
        before = guild.get_channel(40)
        after = fake_text_channel(40, guild, can_read=False)
        guild.channels[guild.channels.index(before)] = after

        await self.call_at(150, cog.on_guild_channel_update, before, after)

        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertIsNone(config.sod_eod_channel_id)
        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, 1, 1), sessions_before
        )
        self.assertEqual(await cog._store_call(cog.store.list_runs, 1), runs_before)

    async def test_bot_role_permission_update_invalidates_reading_and_study(self):
        for field, channel_id, kind in (
            ("reading_category_id", 20, "reading_room"),
            ("study_category_id", 30, "study"),
        ):
            with self.subTest(field=field):
                cog, guild, member = self.make_cog()
                access_role = fake_role(50, guild)
                guild.roles.append(access_role)
                guild.me.roles = [access_role]
                active_member = member.in_category(channel_id)
                await cog.reconcile_member(active_member, 100)
                guild.get_channel(channel_id).permissions_for.return_value = mock.Mock(
                    view_channel=False
                )

                await self.call_at(
                    150,
                    cog.on_guild_role_update,
                    fake_role(50, guild),
                    access_role,
                )

                config = await cog._store_call(cog.store.get_config, guild.id)
                self.assertIsNone(getattr(config, field))
                self.assertFalse(cog.collection_gates[guild.id].is_set())
                self.assertNotIn(guild.id, cog.dirty_guilds)
                self.assertEqual(
                    await session_rows(cog, member.id),
                    [(kind, 100, 150, "config_changed")],
                )
                self.assertEqual(
                    await cog._store_call(cog.store.list_runs, guild.id),
                    [(1, 1, 150, "config_invalid")],
                )

    async def test_bot_role_permission_update_sod_loss_preserves_voice_gate_and_counts(self):
        cog, guild, member = self.make_cog()
        access_role = fake_role(50, guild)
        guild.roles.append(access_role)
        guild.me.roles = [access_role]
        await cog.reconcile_member(member, 100)
        sessions_before = await cog._store_call(cog.store.list_sessions, 1, 1)
        runs_before = await cog._store_call(cog.store.list_runs, 1)
        count_before = await cog._store_call(
            cog.store.voice_session_count_for_range,
            1,
            1,
            "reading_room",
            0,
            200,
        )
        guild.get_channel(40).permissions_for.return_value = mock.Mock(
            view_channel=True,
            read_message_history=False,
        )

        await self.call_at(
            150,
            cog.on_guild_role_update,
            fake_role(50, guild),
            access_role,
        )

        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertIsNone(config.sod_eod_channel_id)
        self.assertTrue(cog.collection_gates[guild.id].is_set())
        self.assertNotIn(guild.id, cog.dirty_guilds)
        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, 1, 1), sessions_before
        )
        self.assertEqual(await cog._store_call(cog.store.list_runs, 1), runs_before)
        self.assertEqual(
            await cog._store_call(
                cog.store.voice_session_count_for_range,
                1,
                1,
                "reading_room",
                0,
                200,
            ),
            count_before,
        )

    async def test_bot_member_role_assignment_change_revalidates_voice_access(self):
        cog, guild, member = self.make_cog()
        access_role = fake_role(50, guild)
        guild.roles.append(access_role)
        before_bot = FakeMember(999, guild, {50}, bot=True)
        after_bot = FakeMember(999, guild, set(), bot=True)
        guild.me = after_bot
        await cog.reconcile_member(member, 100)
        guild.get_channel(20).permissions_for.return_value = mock.Mock(
            view_channel=False
        )

        await self.call_at(150, cog.on_member_update, before_bot, after_bot)

        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertIsNone(config.reading_category_id)
        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 150, "config_changed")],
        )

    async def test_member_remove_marks_dirty_without_writing_when_gate_is_closed(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 100)
        cog.collection_gates[guild.id].clear()

        await self.call_at(150, cog.on_member_remove, member)

        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, None, None)],
        )
        self.assertIn(guild.id, cog.dirty_guilds)

    async def test_incomplete_to_complete_and_complete_to_incomplete_converge(self):
        cog, guild, member = self.make_cog()
        await cog._store_call(
            cog.store.apply_config_change,
            guild.id,
            study_category_id=None,
            effective_at_epoch=50,
        )
        await cog.full_reconcile_guild(guild, 60)
        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(await session_rows(cog, member.id), [])

        await cog._change_voice_setting(guild, 100, study_category_id=30)
        self.assertTrue(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, None, None)],
        )

        deleted = guild.get_channel(30)
        guild.channels.remove(deleted)
        await self.call_at(150, cog.on_guild_channel_delete, deleted)
        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 150, "config_changed")],
        )

    async def test_voice_listener_waits_for_guild_lock_before_reconcile(self):
        cog, guild, member = self.make_cog()
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_lock():
            async with cog.guild_locks[guild.id]:
                holder_entered.set()
                await release_holder.wait()

        holder = asyncio.create_task(hold_lock())
        await holder_entered.wait()
        listener = asyncio.create_task(
            self.call_at(
                100,
                cog.on_voice_state_update,
                member,
                object(),
                object(),
            )
        )
        await asyncio.sleep(0)

        self.assertFalse(listener.done())
        self.assertEqual(await session_rows(cog, member.id), [])

        release_holder.set()
        await asyncio.gather(holder, listener)
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, None, None)],
        )

    async def test_incomplete_full_reconcile_closes_stale_open_rows(self):
        cog, guild, member = self.make_cog()
        await cog._store_call(
            cog.store.apply_config_change,
            guild.id,
            study_category_id=None,
            effective_at_epoch=50,
        )
        await cog._store_call(cog.store.open_collection_run, guild.id, 60)
        await cog._store_call(
            cog.store.reconcile_session,
            guild.id,
            member.id,
            "reading_room",
            60,
        )
        cog.collection_gates[guild.id].set()

        await cog.full_reconcile_guild(guild, 100)

        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 60, 100, "config_changed")],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [
                (1, 1, 50, "config_changed"),
                (60, 60, 100, "config_invalid"),
            ],
        )

    async def test_validation_failure_aborts_open_rows_and_keeps_gate_closed(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 60)
        error = sqlite3.Error("validation unavailable")

        with mock.patch.object(
            cog,
            "_invalidate_configured_resources_locked",
            side_effect=error,
        ), self.assertRaises(sqlite3.Error) as raised:
            await cog.full_reconcile_guild(guild, 100)

        self.assertIs(raised.exception, error)
        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 60, 100, "config_changed")],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [(1, 1, 100, "config_invalid")],
        )

    async def test_voice_resource_invalidation_failures_clear_gate_first(self):
        cases = ("role_delete", "channel_delete", "channel_update")
        for case in cases:
            with self.subTest(case=case):
                cog, guild, member = self.make_cog()
                await cog.reconcile_member(member, 50)
                error = sqlite3.Error("invalidation unavailable")
                with mock.patch.object(
                    cog.store,
                    "invalidate_voice_config",
                    side_effect=error,
                ), self.assertRaises(sqlite3.Error) as raised:
                    if case == "role_delete":
                        await self.call_at(
                            100, cog.on_guild_role_delete, guild.get_role(10)
                        )
                    elif case == "channel_delete":
                        await self.call_at(
                            100, cog.on_guild_channel_delete, guild.get_channel(20)
                        )
                    else:
                        await self.call_at(
                            100,
                            cog.on_guild_channel_update,
                            guild.get_channel(20),
                            fake_text_channel(20, guild),
                        )

                self.assertIs(raised.exception, error)
                self.assertFalse(cog.collection_gates[guild.id].is_set())
                self.assertEqual(
                    await session_rows(cog, member.id),
                    [("reading_room", 50, 100, "config_changed")],
                )
                self.assertEqual(
                    await cog._store_call(cog.store.list_runs, guild.id),
                    [(1, 1, 100, "config_invalid")],
                )

    async def test_full_reconcile_serializes_before_concurrent_config_change(self):
        cog, guild, member = self.make_cog()
        reconcile_entered = asyncio.Event()
        release_reconcile = asyncio.Event()
        original_validate = cog._invalidate_configured_resources_locked

        async def pause_validation(*args, **kwargs):
            reconcile_entered.set()
            await release_reconcile.wait()
            return await original_validate(*args, **kwargs)

        with mock.patch.object(
            cog,
            "_invalidate_configured_resources_locked",
            side_effect=pause_validation,
        ):
            reconcile = asyncio.create_task(cog.full_reconcile_guild(guild, 100))
            await reconcile_entered.wait()
            change = asyncio.create_task(
                cog._change_voice_setting(guild, 150, reading_category_id=21)
            )
            await asyncio.sleep(0)
            self.assertFalse(change.done())
            release_reconcile.set()
            await asyncio.gather(reconcile, change)

        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertEqual(config.reading_category_id, 21)
        self.assertTrue(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 150, "config_changed")],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [(1, 1, 150, "config_changed"), (150, 150, None, None)],
        )


class RecoveryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog, guild, member = configured_fixture()
        self.addCleanup(cog._test_tmp.cleanup)
        return cog, guild, member

    async def test_recovery_waits_until_ready_before_capturing_now(self):
        cog, _guild, _member = self.make_cog()
        ready_entered = asyncio.Event()
        release_ready = asyncio.Event()

        async def wait_until_ready():
            ready_entered.set()
            await release_ready.wait()

        cog.bot.wait_until_ready = wait_until_ready
        with mock.patch("activity_cog.utc_now_epoch", return_value=100) as now:
            recovery = asyncio.create_task(cog.recover_after_ready())
            await ready_entered.wait()
            self.assertEqual(now.call_count, 0)
            release_ready.set()
            await recovery

        self.assertGreaterEqual(now.call_count, 1)

    async def test_guild_available_during_ready_wait_owns_startup_snapshot_recovery(self):
        cog, guild, member = self.make_cog()
        await cog._store_call(
            cog.store.reconcile_session,
            guild.id,
            member.id,
            "study",
            50,
        )
        await cog._store_call(cog.store.checkpoint_open_rows, guild.id, 80)
        guild.members = [member.in_category(20)]
        ready_entered = asyncio.Event()
        release_ready = asyncio.Event()

        async def wait_until_ready():
            ready_entered.set()
            await release_ready.wait()

        cog.bot.wait_until_ready = wait_until_ready
        recovery = asyncio.create_task(cog.recover_after_ready())
        await ready_entered.wait()
        with mock.patch("activity_cog.utc_now_epoch", return_value=100):
            await cog.on_guild_available(guild)
        release_ready.set()
        await recovery

        self.assertEqual(
            await session_rows(cog, member.id),
            [
                ("study", 50, 80, "restart_checkpoint"),
                ("reading_room", 100, None, None),
            ],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [
                (1, 80, 80, "restart_checkpoint"),
                (100, 100, None, None),
            ],
        )
        self.assertEqual(cog._startup_recovered_guild_ids, {guild.id})

    async def test_startup_delta_backfill_recovers_message_after_completed_cursor(self):
        cog, guild, member = self.make_cog()
        await prepare_sync_marker(cog, channel_id=40, message_id=10)
        await cog._store_call(cog.store.mark_backfill_completed, 1, 40, 90)
        channel = make_history_channel(
            guild,
            40,
            [SodEodCollectionTests.full_message(11, member, guild.get_channel(40))],
        )
        guild.channels = [item for item in guild.channels if item.id != 40] + [channel]

        await recover_after_ready_for_test(cog, 100)

        channel.history.assert_called_once_with(
            limit=None,
            oldest_first=True,
            after=discord.Object(id=10),
        )
        self.assertEqual(SodEodCollectionTests.count_events(cog), 1)
        state = await sync_state(cog, 40)
        self.assertEqual(state.newest_processed_message_id, 11)
        self.assertIsNotNone(state.completed_epoch)

    async def test_disconnect_resume_delta_backfill_recovers_downtime_message(self):
        cog, guild, member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)
        await prepare_sync_marker(cog, channel_id=40, message_id=20)
        await cog._store_call(cog.store.mark_backfill_completed, 1, 40, 120)

        await on_disconnect_for_test(cog, 160)
        self.assertIsNone((await sync_state(cog, 40)).completed_epoch)
        channel = make_history_channel(
            guild,
            40,
            [SodEodCollectionTests.full_message(21, member, guild.get_channel(40))],
        )
        guild.channels = [item for item in guild.channels if item.id != 40] + [channel]
        await on_resumed_for_test(cog, 200)

        channel.history.assert_called_once_with(
            limit=None,
            oldest_first=True,
            after=discord.Object(id=20),
        )
        self.assertEqual(SodEodCollectionTests.count_events(cog), 1)
        state = await sync_state(cog, 40)
        self.assertEqual(state.newest_processed_message_id, 21)
        self.assertIsNotNone(state.completed_epoch)

    async def test_guild_available_delta_backfill_recovers_guild_outage_message(self):
        cog, guild, member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)
        await prepare_sync_marker(cog, channel_id=40, message_id=30)
        await cog._store_call(cog.store.mark_backfill_completed, 1, 40, 120)
        channel = make_history_channel(
            guild,
            40,
            [SodEodCollectionTests.full_message(31, member, guild.get_channel(40))],
        )
        guild.channels = [item for item in guild.channels if item.id != 40] + [channel]

        with mock.patch("activity_cog.utc_now_epoch", return_value=200):
            await cog.on_guild_available(guild)

        channel.history.assert_called_once_with(
            limit=None,
            oldest_first=True,
            after=discord.Object(id=30),
        )
        self.assertEqual(SodEodCollectionTests.count_events(cog), 1)
        state = await sync_state(cog, 40)
        self.assertEqual(state.newest_processed_message_id, 31)
        self.assertIsNotNone(state.completed_epoch)

    async def test_failed_resume_delta_backfill_leaves_partial_warning(self):
        cog, guild, _member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)
        await cog._store_call(cog.store.mark_backfill_completed, 1, 40, 120)
        await on_disconnect_for_test(cog, 160)
        channel = fake_text_channel(40, guild)

        async def failed_history(**_kwargs):
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden", headers={}),
                "history unavailable",
            )
            yield None

        channel.history.side_effect = failed_history
        guild.channels = [item for item in guild.channels if item.id != 40] + [channel]
        with mock.patch("activity_cog.logger.exception"):
            await on_resumed_for_test(cog, 200)

        self.assertIsNone((await sync_state(cog, 40)).completed_epoch)
        report = await cog._store_call(
            cog.store.build_report,
            guild_id=1,
            members=[],
            start_epoch=1,
            end_epoch=220,
            as_of_epoch=220,
        )
        self.assertIn("sod_backfill_incomplete", {item.code for item in report.warnings})

    async def test_later_disconnect_wins_over_startup_recovery_waiting_for_ready(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 50)
        ready_entered = asyncio.Event()
        release_ready = asyncio.Event()

        async def wait_until_ready():
            ready_entered.set()
            await release_ready.wait()

        cog.bot.wait_until_ready = wait_until_ready
        with mock.patch("activity_cog.utc_now_epoch", return_value=100):
            recovery = asyncio.create_task(cog.recover_after_ready())
            await ready_entered.wait()
            with mock.patch("activity_cog.utc_now_epoch", return_value=220):
                await cog.on_disconnect()
            release_ready.set()
            await recovery

        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(cog._disconnect_epochs, {guild.id: 220})
        self.assertEqual(
            await cog._store_call(cog.store.count_open_sessions, guild.id),
            0,
        )
        self.assertIsNone(await run_checkpoint(cog, guild.id))

    async def test_later_disconnect_wins_over_queued_resume(self):
        cog, guild, member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_lock():
            async with cog.guild_locks[guild.id]:
                holder_entered.set()
                await release_holder.wait()

        holder = asyncio.create_task(hold_lock())
        await holder_entered.wait()
        with mock.patch("activity_cog.utc_now_epoch", return_value=200):
            resume = asyncio.create_task(cog.on_resumed())
            await asyncio.sleep(0)
        with mock.patch("activity_cog.utc_now_epoch", return_value=220):
            disconnect = asyncio.create_task(cog.on_disconnect())
            await asyncio.sleep(0)
        release_holder.set()
        await asyncio.gather(holder, resume, disconnect)

        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(cog._disconnect_epochs, {guild.id: 220})
        self.assertEqual(
            await cog._store_call(cog.store.count_open_sessions, guild.id),
            0,
        )
        self.assertIsNone(await run_checkpoint(cog, guild.id))

    async def test_later_disconnect_wins_over_queued_guild_available(self):
        cog, guild, _member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_lock():
            async with cog.guild_locks[guild.id]:
                holder_entered.set()
                await release_holder.wait()

        holder = asyncio.create_task(hold_lock())
        await holder_entered.wait()
        with mock.patch("activity_cog.utc_now_epoch", return_value=200):
            available = asyncio.create_task(cog.on_guild_available(guild))
            await asyncio.sleep(0)
        with mock.patch("activity_cog.utc_now_epoch", return_value=220):
            disconnect = asyncio.create_task(cog.on_disconnect())
            await asyncio.sleep(0)
        release_holder.set()
        await asyncio.gather(holder, available, disconnect)

        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(cog._disconnect_epochs, {guild.id: 220})
        self.assertEqual(
            await cog._store_call(cog.store.count_open_sessions, guild.id),
            0,
        )
        self.assertIsNone(await run_checkpoint(cog, guild.id))

    async def _disconnect_after_reconcile_observes_open_rows(
        self,
        cog,
        guild,
        lifecycle_callback,
    ):
        store_entered = threading.Event()
        release_store = threading.Event()
        original_list_open = cog.store.list_open_session_user_ids

        def pause_after_observing_open_rows(guild_id):
            result = original_list_open(guild_id)
            store_entered.set()
            release_store.wait(5)
            return result

        with mock.patch.object(
            cog.store,
            "list_open_session_user_ids",
            side_effect=pause_after_observing_open_rows,
        ):
            with mock.patch("activity_cog.utc_now_epoch", return_value=200):
                lifecycle = asyncio.create_task(lifecycle_callback())
                while not store_entered.is_set():
                    await asyncio.sleep(0)
            with mock.patch("activity_cog.utc_now_epoch", return_value=220):
                disconnect = asyncio.create_task(cog.on_disconnect())
                await asyncio.sleep(0)
                self.assertFalse(cog.collection_gates[guild.id].is_set())
                release_store.set()
                await asyncio.gather(lifecycle, disconnect)

    async def test_disconnect_owns_existing_rows_after_stale_guild_available(self):
        cog, guild, member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)
        await cog._store_call(cog.store.checkpoint_open_rows, guild.id, 120)

        await self._disconnect_after_reconcile_observes_open_rows(
            cog,
            guild,
            lambda: cog.on_guild_available(guild),
        )

        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 220, "gateway_disconnect")],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [
                (1, 1, 1, "restart_checkpoint"),
                (100, 120, 220, "gateway_disconnect"),
            ],
        )
        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(cog._disconnect_epochs, {guild.id: 220})

    async def test_disconnect_cleans_rows_opened_by_stale_resume(self):
        cog, guild, member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)

        await self._disconnect_after_reconcile_observes_open_rows(
            cog,
            guild,
            cog.on_resumed,
        )

        self.assertEqual(
            await session_rows(cog, member.id),
            [
                ("reading_room", 100, 100, "restart_checkpoint"),
                ("reading_room", 200, 220, "gateway_disconnect"),
            ],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [
                (1, 1, 1, "restart_checkpoint"),
                (100, 100, 100, "restart_checkpoint"),
                (200, 200, 220, "gateway_disconnect"),
            ],
        )
        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(cog._disconnect_epochs, {guild.id: 220})

    async def test_stale_reconcile_failure_leaves_rows_for_later_disconnect(self):
        cog, guild, member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)
        await cog._store_call(cog.store.checkpoint_open_rows, guild.id, 120)
        stale_attempt_member = FakeMember(2, guild, {10}, 20)
        guild.members.append(stale_attempt_member)
        store_entered = threading.Event()
        release_store = threading.Event()
        original_list_open = cog.store.list_open_session_user_ids
        error = sqlite3.Error("late reconcile failure")

        def fail_after_observing_open_rows(guild_id):
            original_list_open(guild_id)
            store_entered.set()
            release_store.wait(5)
            raise error

        with mock.patch.object(
            cog.store,
            "list_open_session_user_ids",
            side_effect=fail_after_observing_open_rows,
        ), mock.patch("activity_cog.logger.exception") as log_exception:
            with mock.patch("activity_cog.utc_now_epoch", return_value=200):
                available = asyncio.create_task(cog.on_guild_available(guild))
                while not store_entered.is_set():
                    await asyncio.sleep(0)
            with mock.patch("activity_cog.utc_now_epoch", return_value=220):
                disconnect = asyncio.create_task(cog.on_disconnect())
                await asyncio.sleep(0)
                self.assertFalse(cog.collection_gates[guild.id].is_set())
                release_store.set()
                await asyncio.gather(available, disconnect)

        log_exception.assert_called_once_with(
            "activity guild-available recovery failed for guild %s",
            guild.id,
        )
        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 220, "gateway_disconnect")],
        )
        self.assertEqual(
            await session_rows(cog, stale_attempt_member.id),
            [("reading_room", 200, 220, "gateway_disconnect")],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [
                (1, 1, 1, "restart_checkpoint"),
                (100, 120, 220, "gateway_disconnect"),
            ],
        )
        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(cog._disconnect_epochs, {guild.id: 220})

    async def test_hard_restart_closes_at_checkpoint_and_starts_fresh_at_now(self):
        cog, guild, member = self.make_cog()
        await cog._store_call(
            cog.store.reconcile_session,
            guild.id,
            member.id,
            "study",
            50,
        )
        await cog._store_call(cog.store.checkpoint_open_rows, guild.id, 80)
        guild.members = [member.in_category(20)]

        await recover_after_ready_for_test(cog, 100)

        self.assertEqual(
            await session_rows(cog, member.id),
            [
                ("study", 50, 80, "restart_checkpoint"),
                ("reading_room", 100, None, None),
            ],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [
                (1, 80, 80, "restart_checkpoint"),
                (100, 100, None, None),
            ],
        )
        self.assertTrue(cog.collection_gates[guild.id].is_set())

    async def test_recovery_snapshot_does_not_close_row_inserted_after_snapshot(self):
        cog, guild, member = self.make_cog()
        await cog._store_call(
            cog.store.reconcile_session,
            guild.id,
            member.id,
            "study",
            50,
        )
        guild.members.append(FakeMember(2, guild, {10}, 20))
        original_snapshot = cog.store.snapshot_open_row_ids

        def snapshot_then_insert(guild_id):
            snapshot = original_snapshot(guild_id)
            cog.store.reconcile_session(1, 2, "reading_room", 80)
            return snapshot

        with mock.patch.object(
            cog.store,
            "snapshot_open_row_ids",
            side_effect=snapshot_then_insert,
        ):
            await recover_after_ready_for_test(cog, 100)

        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, guild.id, 2),
            [("reading_room", 80, None, None)],
        )

    async def test_recovery_reconcile_writes_while_gate_is_closed(self):
        cog, guild, member = self.make_cog()
        cog.collection_gates[guild.id].clear()

        await recover_after_ready_for_test(cog, 100)

        self.assertEqual(
            await cog._store_call(
                cog.store.open_session_count,
                guild.id,
                member.id,
            ),
            1,
        )

    async def test_disconnect_gap_ignores_voice_event_until_resume(self):
        cog, guild, member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)

        await on_disconnect_for_test(cog, 160)
        await cog.on_voice_state_update(
            member.in_category(30),
            object(),
            object(),
        )
        guild.members = [member.in_category(30)]
        await on_resumed_for_test(cog, 200)

        self.assertEqual(await coverage_gaps(cog, 100, 240), [(160, 200)])
        self.assertEqual(
            await session_rows(cog, member.id),
            [
                ("reading_room", 100, 160, "gateway_disconnect"),
                ("study", 200, None, None),
            ],
        )

    async def test_repeated_disconnect_resume_cycles_are_idempotent(self):
        cog, guild, _member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)

        await on_disconnect_for_test(cog, 160)
        await on_disconnect_for_test(cog, 180)
        await on_resumed_for_test(cog, 200)
        await on_disconnect_for_test(cog, 220)
        await on_disconnect_for_test(cog, 230)
        await on_resumed_for_test(cog, 240)

        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [
                (1, 1, 1, "restart_checkpoint"),
                (100, 100, 160, "gateway_disconnect"),
                (200, 200, 220, "gateway_disconnect"),
                (240, 240, None, None),
            ],
        )

    async def test_guild_available_repairs_failed_disconnect_and_resets_epoch(self):
        cog, guild, member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)
        await cog._store_call(cog.store.checkpoint_open_rows, guild.id, 120)
        with mock.patch.object(
            cog.store,
            "close_open_rows",
            side_effect=sqlite3.Error("disconnect close unavailable"),
        ), mock.patch("activity_cog.logger.exception"):
            await on_disconnect_for_test(cog, 160)

        with mock.patch("activity_cog.utc_now_epoch", return_value=200):
            await cog.on_guild_available(guild)

        self.assertNotIn(guild.id, cog._disconnect_epochs)
        self.assertTrue(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await session_rows(cog, member.id),
            [
                ("reading_room", 100, 120, "gateway_disconnect"),
                ("reading_room", 200, None, None),
            ],
        )
        await on_disconnect_for_test(cog, 240)
        self.assertEqual(cog._disconnect_epochs, {guild.id: 240})
        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await session_rows(cog, member.id),
            [
                ("reading_room", 100, 120, "gateway_disconnect"),
                ("reading_room", 200, 240, "gateway_disconnect"),
            ],
        )

    async def test_disconnect_waiting_on_listener_lock_leaves_no_open_row(self):
        cog, guild, member = self.make_cog()
        await recover_after_ready_for_test(cog, 100)
        store_entered = threading.Event()
        release_store = threading.Event()
        original_reconcile = cog.store.reconcile_session

        def pause_store(*args, **kwargs):
            store_entered.set()
            release_store.wait(5)
            return original_reconcile(*args, **kwargs)

        with mock.patch.object(cog.store, "reconcile_session", side_effect=pause_store):
            with mock.patch("activity_cog.utc_now_epoch", return_value=150):
                listener = asyncio.create_task(
                    cog.on_voice_state_update(
                        member.in_category(30), object(), object()
                    )
                )
                while not store_entered.is_set():
                    await asyncio.sleep(0)
            with mock.patch("activity_cog.utc_now_epoch", return_value=160):
                disconnect = asyncio.create_task(cog.on_disconnect())
                await asyncio.sleep(0)
                self.assertFalse(cog.collection_gates[guild.id].is_set())
                release_store.set()
                await asyncio.gather(listener, disconnect)

        self.assertEqual(
            await cog._store_call(cog.store.count_open_sessions, guild.id),
            0,
        )

    async def test_closed_gate_does_not_checkpoint(self):
        cog, guild, _member = self.make_cog()
        before = await run_checkpoint(cog, guild.id)
        cog.collection_gates[guild.id].clear()

        with mock.patch("activity_cog.utc_now_epoch", return_value=100):
            await checkpoint_once_for_test(cog)

        self.assertEqual(await run_checkpoint(cog, guild.id), before)

    async def test_checkpoint_captures_now_per_open_guild_after_lock(self):
        cog, guild, _member = self.make_cog()
        second = FakeGuild(2)
        cog.bot.guilds.append(second)
        await cog._store_call(cog.store.open_collection_run, second.id, 200)
        cog.collection_gates[second.id].set()

        with mock.patch("activity_cog.utc_now_epoch", side_effect=[150, 250]):
            await checkpoint_once_for_test(cog)

        self.assertEqual(await run_checkpoint(cog, guild.id), 150)
        self.assertEqual(await run_checkpoint(cog, second.id), 250)

        with mock.patch("activity_cog.utc_now_epoch", side_effect=[140, 240]):
            await checkpoint_once_for_test(cog)

        self.assertEqual(await run_checkpoint(cog, guild.id), 150)
        self.assertEqual(await run_checkpoint(cog, second.id), 250)

    async def test_cog_load_and_unload_cancel_and_await_owned_tasks(self):
        cog, _guild, _member = self.make_cog()
        recovery_started = asyncio.Event()

        async def blocked_recovery():
            recovery_started.set()
            await asyncio.Event().wait()

        with mock.patch.object(cog, "recover_after_ready", side_effect=blocked_recovery):
            await cog.cog_load()
            await recovery_started.wait()
            recovery_task = cog.recovery_task
            checkpoint_task = cog.checkpoint_task

            await cog.cog_unload()

        self.assertTrue(recovery_task.done())
        self.assertTrue(checkpoint_task.done())
        self.assertEqual(cog._lifecycle_tasks, set())

    async def test_cog_unload_directly_closes_rows_with_graceful_reasons(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 100)

        with mock.patch("activity_cog.utc_now_epoch", return_value=150):
            await cog.cog_unload()

        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 150, "reconciled")],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [(1, 1, 150, "graceful_shutdown")],
        )

    async def test_cog_unload_waits_for_cancelled_store_worker_before_cleanup(self):
        cog, guild, member = self.make_cog()
        worker_entered = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()

        def late_session_write():
            worker_entered.set()
            release_worker.wait(5)
            cog.store.reconcile_session(
                guild.id,
                member.id,
                "reading_room",
                100,
            )
            worker_finished.set()

        write = asyncio.create_task(cog._store_call(late_session_write))
        while not worker_entered.is_set():
            await asyncio.sleep(0)
        write.cancel()
        with mock.patch("activity_cog.utc_now_epoch", return_value=150):
            unload = asyncio.create_task(cog.cog_unload())
            await asyncio.sleep(0)
            cancellation_and_cleanup_waited = not write.done() and not unload.done()
            release_worker.set()
            with self.assertRaises(asyncio.CancelledError):
                await write
            await unload

        while not worker_finished.is_set():
            await asyncio.sleep(0)
        self.assertTrue(cancellation_and_cleanup_waited)
        self.assertEqual(
            await cog._store_call(cog.store.count_open_sessions, guild.id),
            0,
        )
        self.assertIsNone(await run_checkpoint(cog, guild.id))
        self.assertEqual(cog._store_worker_tasks, set())
        self.assertEqual(cog._lifecycle_tasks, set())

    async def test_cog_unload_logs_cleanup_failure_and_continues(self):
        cog, _guild, _member = self.make_cog()
        error = sqlite3.Error("cleanup unavailable")

        with mock.patch.object(
            cog.store,
            "close_open_rows",
            side_effect=error,
        ), mock.patch("activity_cog.logger.exception") as logged:
            await cog.cog_unload()

        logged.assert_called_once()


class SettingsCommandTests(unittest.IsolatedAsyncioTestCase):
    async def make_cog(self):
        from activity_cog import ActivityCog

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = ActivityStore(str(Path(temp_dir.name) / "activity.db"))
        await asyncio.to_thread(store.initialize)
        guild = FakeGuild(1)
        guild.roles = [fake_role(10, guild)]
        guild.channels = [
            fake_category(20, guild),
            fake_category(30, guild),
            fake_text_channel(40, guild),
        ]
        guild.members = [FakeMember(1, guild, {10}, 20, display_name="Secret Name")]
        bot = FakeBot()
        bot.guilds = [guild]
        return ActivityCog(bot, store), guild

    async def invoke(self, command, cog, interaction, resource=None, *, now=100):
        arguments = (cog, interaction) if resource is None else (cog, interaction, resource)
        with mock.patch("activity_cog.utc_now_epoch", return_value=now):
            await command.callback(*arguments)

    async def set_complete_config(self, cog, guild, *, now=1):
        await cog._store_call(
            cog.store.apply_config_change,
            guild.id,
            target_role_id=10,
            reading_category_id=20,
            study_category_id=30,
            sod_eod_channel_id=40,
            effective_at_epoch=now,
        )
        cog.collection_gates[guild.id].set()
        await cog._store_call(cog.store.open_collection_run, guild.id, now)

    def test_settings_group_is_guild_only_with_admin_registration_hint(self):
        from activity_cog import ActivityCog

        self.assertTrue(ActivityCog.settings_group.guild_only)
        self.assertTrue(ActivityCog.settings_group.default_permissions.administrator)

    async def test_every_setting_order_reaches_the_same_complete_configuration(self):
        setters = ("target_role", "reading", "study", "sod_eod")
        for order in itertools.permutations(setters):
            with self.subTest(order=order):
                cog, guild = await self.make_cog()
                resources = {
                    "target_role": (cog.set_target_role, guild.roles[0]),
                    "reading": (cog.set_reading_category, guild.channels[0]),
                    "study": (cog.set_study_category, guild.channels[1]),
                    "sod_eod": (cog.set_sod_eod_channel, guild.channels[2]),
                }
                for index, name in enumerate(order):
                    command, resource = resources[name]
                    await self.invoke(
                        command,
                        cog,
                        fake_interaction(1, True, guild),
                        resource,
                        now=100 + index,
                    )

                config = await cog._store_call(cog.store.get_config, guild.id)
                self.assertEqual(
                    (
                        config.target_role_id,
                        config.reading_category_id,
                        config.study_category_id,
                        config.sod_eod_channel_id,
                    ),
                    (10, 20, 30, 40),
                )
                self.assertTrue(cog.collection_gates[guild.id].is_set())
                self.assertEqual(
                    await cog._store_call(cog.store.open_session_count, guild.id, 1),
                    1,
                )

    async def test_non_admin_and_dm_guards_run_before_every_settings_mutation(self):
        cog, guild = await self.make_cog()
        cases = (
            (cog.set_target_role, guild.roles[0]),
            (cog.set_reading_category, guild.channels[0]),
            (cog.set_study_category, guild.channels[1]),
            (cog.set_sod_eod_channel, guild.channels[2]),
            (cog.activity_status, None),
        )
        original = await cog._store_call(cog.store.get_config, guild.id)
        for command, resource in cases:
            with self.subTest(command=command.name, rejection="non-admin"):
                interaction = fake_interaction(2, False, guild)
                await self.invoke(command, cog, interaction, resource)
                self.assertTrue(interaction.response.sent[0][1]["ephemeral"])
                self.assertFalse(interaction.response.deferred)
                self.assertEqual(
                    await cog._store_call(cog.store.get_config, guild.id), original
                )

            with self.subTest(command=command.name, rejection="dm"):
                interaction = fake_interaction(1, True, guild)
                interaction.guild = None
                await self.invoke(command, cog, interaction, resource)
                self.assertTrue(interaction.response.sent[0][1]["ephemeral"])
                self.assertFalse(interaction.response.deferred)
                self.assertEqual(
                    await cog._store_call(cog.store.get_config, guild.id), original
                )

    async def test_admin_guard_uses_ephemeral_followup_after_initial_response(self):
        cog, guild = await self.make_cog()
        interaction = fake_interaction(2, False, guild)
        interaction.response.done = True

        await self.invoke(cog.activity_status, cog, interaction)

        self.assertEqual(interaction.response.sent, [])
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertTrue(interaction.followup.sent[0][1]["ephemeral"])

    async def test_setters_reject_wrong_type_cross_guild_and_same_categories(self):
        cog, guild = await self.make_cog()
        await self.set_complete_config(cog, guild)
        other = FakeGuild(2)
        invalid_cases = (
            (cog.set_target_role, fake_text_channel(41, guild)),
            (cog.set_reading_category, fake_text_channel(42, guild)),
            (cog.set_study_category, fake_text_channel(43, guild)),
            (cog.set_sod_eod_channel, fake_category(44, guild)),
            (cog.set_target_role, fake_role(50, other)),
            (cog.set_reading_category, fake_category(51, other)),
            (cog.set_study_category, fake_category(52, other)),
            (cog.set_sod_eod_channel, fake_text_channel(53, other)),
            (cog.set_reading_category, guild.channels[1]),
            (cog.set_study_category, guild.channels[0]),
        )
        original = await cog._store_call(cog.store.get_config, guild.id)
        for command, resource in invalid_cases:
            with self.subTest(command=command.name, resource=resource.id):
                interaction = fake_interaction(1, True, guild)
                await self.invoke(command, cog, interaction, resource, now=200)
                self.assertTrue(interaction.response.deferred)
                self.assertTrue(interaction.response.defer_kwargs["ephemeral"])
                self.assertEqual(len(interaction.original_edits), 1)
                self.assertEqual(
                    await cog._store_call(cog.store.get_config, guild.id), original
                )

    async def test_admin_callbacks_defer_ephemeral_before_first_store_call(self):
        cog, guild = await self.make_cog()
        cases = (
            (cog.set_target_role, guild.roles[0]),
            (cog.set_reading_category, guild.channels[0]),
            (cog.set_study_category, guild.channels[1]),
            (cog.set_sod_eod_channel, guild.channels[2]),
            (cog.activity_status, None),
        )
        for command, resource in cases:
            with self.subTest(command=command.name):
                interaction = fake_interaction(1, True, guild)
                observations = []
                original_store_call = cog._store_call

                async def observe_defer(method, *args, **kwargs):
                    observations.append(
                        interaction.response.deferred
                        and interaction.response.defer_kwargs.get("ephemeral") is True
                    )
                    return await original_store_call(method, *args, **kwargs)

                with mock.patch.object(cog, "_store_call", side_effect=observe_defer):
                    await self.invoke(command, cog, interaction, resource)

                self.assertTrue(observations)
                self.assertTrue(all(observations))
                self.assertEqual(interaction.response.sent, [])
                self.assertEqual(len(interaction.original_edits), 1)

    async def test_inaccessible_category_and_text_setters_do_not_mutate(self):
        cog, guild = await self.make_cog()
        await self.set_complete_config(cog, guild)
        inaccessible_category = fake_category(21, guild, can_read=False)
        inaccessible_text = fake_text_channel(41, guild)
        inaccessible_text.permissions_for.return_value = mock.Mock(
            view_channel=True,
            read_message_history=False,
        )
        invisible_text = fake_text_channel(42, guild)
        invisible_text.permissions_for.return_value = mock.Mock(
            view_channel=False,
            read_message_history=True,
        )
        guild.channels.extend(
            (inaccessible_category, inaccessible_text, invisible_text)
        )
        original = await cog._store_call(cog.store.get_config, guild.id)

        for command, resource in (
            (cog.set_reading_category, inaccessible_category),
            (cog.set_study_category, inaccessible_category),
            (cog.set_sod_eod_channel, inaccessible_text),
            (cog.set_sod_eod_channel, invisible_text),
        ):
            with self.subTest(command=command.name):
                interaction = fake_interaction(1, True, guild)
                await self.invoke(command, cog, interaction, resource, now=200)

                self.assertTrue(interaction.response.deferred)
                self.assertEqual(len(interaction.original_edits), 1)
                self.assertIn("수 없습니다", interaction.original_edits[0]["content"])
                self.assertEqual(
                    await cog._store_call(cog.store.get_config, guild.id), original
                )

    async def test_sod_only_change_preserves_voice_sessions_and_runs_exactly(self):
        cog, guild = await self.make_cog()
        await self.set_complete_config(cog, guild)
        await cog.reconcile_member(guild.members[0], 100)
        sessions_before = await cog._store_call(cog.store.list_sessions, 1, 1)
        runs_before = await cog._store_call(cog.store.list_runs, 1)
        count_before = await cog._store_call(
            cog.store.voice_session_count_for_range,
            1,
            1,
            "reading_room",
            0,
            200,
        )
        new_channel = fake_text_channel(41, guild)
        guild.channels.append(new_channel)

        await self.invoke(
            cog.set_sod_eod_channel,
            cog,
            fake_interaction(1, True, guild),
            new_channel,
            now=120,
        )

        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, 1, 1), sessions_before
        )
        self.assertEqual(await cog._store_call(cog.store.list_runs, 1), runs_before)
        self.assertEqual(
            await cog._store_call(
                cog.store.voice_session_count_for_range,
                1,
                1,
                "reading_room",
                0,
                200,
            ),
            count_before,
        )

    async def test_voice_change_closes_and_fully_reconciles_at_the_same_epoch(self):
        cog, guild = await self.make_cog()
        await self.set_complete_config(cog, guild)
        await cog.reconcile_member(guild.members[0], 100)
        replacement = fake_category(21, guild)
        guild.channels.append(replacement)
        guild.members = [guild.members[0].in_category(21)]

        await self.invoke(
            cog.set_reading_category,
            cog,
            fake_interaction(1, True, guild),
            replacement,
            now=150,
        )

        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, 1, 1),
            [
                ("reading_room", 100, 150, "config_changed"),
                ("reading_room", 150, None, None),
            ],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, 1),
            [(1, 1, 150, "config_changed"), (150, 150, None, None)],
        )

    async def test_full_reconcile_keeps_gate_closed_when_run_open_fails(self):
        cog, guild = await self.make_cog()
        await cog._store_call(
            cog.store.apply_config_change,
            guild.id,
            target_role_id=10,
            reading_category_id=20,
            study_category_id=30,
            effective_at_epoch=1,
        )
        error = sqlite3.Error("run unavailable")

        with mock.patch.object(
            cog.store, "open_collection_run", side_effect=error
        ), self.assertRaises(sqlite3.Error) as raised:
            await cog.full_reconcile_guild(guild, 101)

        self.assertIs(raised.exception, error)
        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(await cog._store_call(cog.store.list_runs, guild.id), [])
        self.assertEqual(
            await cog._store_call(cog.store.open_session_count, guild.id, 1), 0
        )

    async def test_full_reconcile_aborts_partial_rows_when_member_reconcile_fails(self):
        cog, guild = await self.make_cog()
        await cog._store_call(
            cog.store.apply_config_change,
            guild.id,
            target_role_id=10,
            reading_category_id=20,
            study_category_id=30,
            effective_at_epoch=1,
        )
        guild.members.append(FakeMember(2, guild, {10}, 20))
        original_reconcile = cog.reconcile_member

        async def fail_after_first(member, effective_at_epoch, **kwargs):
            if member.id == 2:
                raise sqlite3.Error("member unavailable")
            await original_reconcile(member, effective_at_epoch, **kwargs)

        with mock.patch.object(cog, "reconcile_member", side_effect=fail_after_first):
            with self.assertRaises(sqlite3.Error):
                await cog.full_reconcile_guild(guild, 101)

        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, guild.id, 1),
            [("reading_room", 101, 101, "config_changed")],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [(101, 101, 101, "config_invalid")],
        )

    async def test_retrying_same_voice_setting_recovers_after_reconcile_failure(self):
        cog, guild = await self.make_cog()
        await self.set_complete_config(cog, guild)
        replacement = fake_category(21, guild)
        guild.channels.append(replacement)
        guild.members = [guild.members[0].in_category(21)]
        first = fake_interaction(1, True, guild)

        with mock.patch.object(
            cog.store,
            "open_collection_run",
            side_effect=sqlite3.Error("run unavailable"),
        ), mock.patch("activity_cog.logger.exception"):
            await self.invoke(
                cog.set_reading_category,
                cog,
                first,
                replacement,
                now=150,
            )
        self.assertFalse(cog.collection_gates[guild.id].is_set())

        second = fake_interaction(1, True, guild)
        await self.invoke(
            cog.set_reading_category,
            cog,
            second,
            replacement,
            now=160,
        )

        self.assertTrue(cog.collection_gates[guild.id].is_set())
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [(1, 1, 150, "config_changed"), (160, 160, None, None)],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, guild.id, 1),
            [("reading_room", 160, None, None)],
        )

    async def test_setter_store_failure_returns_one_ephemeral_error(self):
        cog, guild = await self.make_cog()
        interaction = fake_interaction(1, True, guild)
        error = sqlite3.Error("database unavailable")

        with mock.patch.object(
            cog, "_change_sod_setting", side_effect=error
        ), mock.patch("activity_cog.logger.exception"):
            await self.invoke(
                cog.set_sod_eod_channel,
                cog,
                interaction,
                guild.channels[2],
            )

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.response.sent, [])
        self.assertEqual(len(interaction.original_edits), 1)
        self.assertIn(
            "처리하지 못했습니다", interaction.original_edits[0]["content"]
        )
        self.assertEqual(interaction.followup.sent, [])

    async def test_status_store_failure_edits_deferred_ephemeral_response_once(self):
        cog, guild = await self.make_cog()
        interaction = fake_interaction(1, True, guild)
        error = sqlite3.Error("database unavailable")

        with mock.patch.object(
            cog, "_invalidate_configured_resources", side_effect=error
        ), mock.patch("activity_cog.logger.exception"):
            await self.invoke(cog.activity_status, cog, interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.response.sent, [])
        self.assertEqual(interaction.followup.sent, [])
        self.assertEqual(len(interaction.original_edits), 1)
        self.assertIn(
            "처리하지 못했습니다", interaction.original_edits[0]["content"]
        )

    async def test_status_invalidates_missing_role_with_config_invalid_run(self):
        cog, guild = await self.make_cog()
        await self.set_complete_config(cog, guild)
        await cog.reconcile_member(guild.members[0], 100)
        guild.roles = []
        interaction = fake_interaction(1, True, guild)

        await self.invoke(cog.activity_status, cog, interaction, now=200)

        self.assertTrue(interaction.response.defer_kwargs["ephemeral"])
        content = interaction.original_edits[0]["content"]
        self.assertIn("대상 역할을 찾을 수 없습니다", content)
        self.assertIsNone(
            (await cog._store_call(cog.store.get_config, 1)).target_role_id
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, 1, 1),
            [("reading_room", 100, 200, "config_changed")],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, 1),
            [(1, 1, 200, "config_invalid")],
        )

    async def test_status_invalidates_wrong_category_and_sod_channel_types(self):
        cog, guild = await self.make_cog()
        await self.set_complete_config(cog, guild)
        guild.channels = [
            fake_text_channel(20, guild),
            guild.channels[1],
            fake_category(40, guild),
        ]
        interaction = fake_interaction(1, True, guild)

        await self.invoke(cog.activity_status, cog, interaction, now=210)

        content = interaction.original_edits[0]["content"]
        self.assertIn("독서실 카테고리를 찾을 수 없습니다", content)
        self.assertIn("SoD/EoD 텍스트 채널을 찾을 수 없습니다", content)
        config = await cog._store_call(cog.store.get_config, 1)
        self.assertIsNone(config.reading_category_id)
        self.assertIsNone(config.sod_eod_channel_id)
        with closing(sqlite3.connect(cog.store.db_path)) as conn:
            period = conn.execute(
                "SELECT ended_epoch, ended_reason FROM sod_eod_channel_periods"
            ).fetchone()
        self.assertEqual(period, (210, "config_invalid"))

    async def test_status_tolerates_null_settings_and_exposes_only_operational_data(self):
        cog, guild = await self.make_cog()
        interaction = fake_interaction(1, True, guild)

        await self.invoke(cog.activity_status, cog, interaction, now=220)

        self.assertTrue(interaction.response.defer_kwargs["ephemeral"])
        content = interaction.original_edits[0]["content"]
        self.assertIn("대상 역할 ID: 미설정", content)
        self.assertIn("독서실 카테고리 ID: 미설정", content)
        self.assertIn("스터디 카테고리 ID: 미설정", content)
        self.assertIn("SoD/EoD 채널 ID: 미설정", content)
        self.assertIn("열린 음성 세션: 0", content)
        self.assertIn("열린 수집 run: 0", content)
        self.assertNotIn("Secret Name", content)
        self.assertNotIn("user_id", content)

    async def test_status_invalidates_inaccessible_voice_and_sod_resources(self):
        cog, guild = await self.make_cog()
        await self.set_complete_config(cog, guild)
        await cog.reconcile_member(guild.members[0], 100)
        guild.channels[0].permissions_for.return_value = mock.Mock(
            view_channel=False
        )
        guild.channels[2].permissions_for.return_value = mock.Mock(
            view_channel=True,
            read_message_history=False,
        )
        interaction = fake_interaction(1, True, guild)

        await self.invoke(cog.activity_status, cog, interaction, now=210)

        content = interaction.original_edits[0]["content"]
        self.assertIn("독서실 카테고리에 접근할 수 없습니다", content)
        self.assertIn("SoD/EoD 텍스트 채널에 접근할 수 없습니다", content)
        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertIsNone(config.reading_category_id)
        self.assertIsNone(config.sod_eod_channel_id)
        self.assertEqual(
            await cog._store_call(cog.store.list_sessions, guild.id, 1),
            [("reading_room", 100, 210, "config_changed")],
        )
        self.assertEqual(
            await cog._store_call(cog.store.list_runs, guild.id),
            [(1, 1, 210, "config_invalid")],
        )
        with closing(sqlite3.connect(cog.store.db_path)) as conn:
            period = conn.execute(
                "SELECT ended_epoch, ended_reason FROM sod_eod_channel_periods"
            ).fetchone()
        self.assertEqual(period, (210, "config_invalid"))

    async def test_public_full_reconcile_rejects_inaccessible_categories(self):
        cog, guild = await self.make_cog()
        await cog._store_call(
            cog.store.apply_config_change,
            guild.id,
            target_role_id=10,
            reading_category_id=20,
            study_category_id=30,
            effective_at_epoch=1,
        )

        guild.channels[1].permissions_for.return_value = mock.Mock(
            view_channel=False
        )
        await cog.full_reconcile_guild(guild, 100)

        self.assertFalse(cog.collection_gates[guild.id].is_set())
        self.assertEqual(await cog._store_call(cog.store.list_runs, guild.id), [])
        self.assertEqual(
            await cog._store_call(cog.store.count_open_sessions, guild.id), 0
        )

    async def test_public_full_reconcile_waits_for_other_task_holding_guild_lock(self):
        cog, guild = await self.make_cog()
        await cog._store_call(
            cog.store.apply_config_change,
            guild.id,
            target_role_id=10,
            reading_category_id=20,
            study_category_id=30,
            effective_at_epoch=1,
        )
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        caller_started = asyncio.Event()
        reconcile_entered = asyncio.Event()
        store_entered = asyncio.Event()
        original_reconcile = cog._full_reconcile_guild_locked
        original_store_call = cog._store_call

        async def hold_lock():
            async with cog.guild_locks[guild.id]:
                holder_entered.set()
                await release_holder.wait()

        async def record_store_call(method, *args, **kwargs):
            store_entered.set()
            return await original_store_call(method, *args, **kwargs)

        async def record_reconcile(*args, **kwargs):
            reconcile_entered.set()
            return await original_reconcile(*args, **kwargs)

        async def call_public_reconcile():
            caller_started.set()
            await cog.full_reconcile_guild(guild, 100)

        holder = asyncio.create_task(hold_lock())
        await holder_entered.wait()
        with mock.patch.object(
            cog, "_full_reconcile_guild_locked", side_effect=record_reconcile
        ), mock.patch.object(cog, "_store_call", side_effect=record_store_call):
            caller = asyncio.create_task(call_public_reconcile())
            await caller_started.wait()
            was_blocked = (
                not reconcile_entered.is_set()
                and not store_entered.is_set()
                and not caller.done()
            )
            release_holder.set()
            await asyncio.gather(holder, caller)

        self.assertTrue(was_blocked)
        self.assertTrue(reconcile_entered.is_set())
        self.assertTrue(store_entered.is_set())
        self.assertTrue(cog.collection_gates[guild.id].is_set())

    async def test_long_status_is_bounded_and_preserves_detail_in_utf8_attachment(self):
        cog, guild = await self.make_cog()
        await self.set_complete_config(cog, guild)
        with closing(sqlite3.connect(cog.store.db_path)) as conn:
            conn.executemany(
                """
                INSERT INTO voice_collection_runs(
                    guild_id, started_epoch, last_checkpoint_epoch,
                    ended_epoch, ended_reason
                ) VALUES (1, ?, ?, ?, 'config_changed')
                """,
                [
                    (1000 + index * 10, 1000 + index * 10, 1005 + index * 10)
                    for index in range(80)
                ],
            )
            conn.commit()
        interaction = fake_interaction(1, True, guild)

        await self.invoke(cog.activity_status, cog, interaction, now=3000)

        self.assertEqual(interaction.response.sent, [])
        self.assertEqual(interaction.followup.sent, [])
        self.assertEqual(len(interaction.original_edits), 1)
        edit = interaction.original_edits[0]
        self.assertLessEqual(len(edit["content"]), 2000)
        self.assertEqual(len(edit["attachments"]), 1)
        attachment = edit["attachments"][0]
        self.assertTrue(attachment.filename.endswith(".txt"))
        detail = attachment.fp.getvalue().decode("utf-8")
        self.assertGreater(len(detail), 2000)
        self.assertIn("시작=1000", detail)
        self.assertIn("시작=1790", detail)


class SodEodCollectionTests(unittest.IsolatedAsyncioTestCase):
    def make_fixture(self):
        cog, guild, member = configured_fixture()
        self.addCleanup(cog._test_tmp.cleanup)
        return cog, guild, member

    @staticmethod
    def count_events(cog):
        with closing(sqlite3.connect(cog.store.db_path)) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM sod_eod_events").fetchone()[0])

    @staticmethod
    def full_message(
        message_id,
        author,
        channel,
        content="SoD",
        *,
        guild=None,
        message_type=discord.MessageType.default,
        webhook_id=None,
        created_at=None,
    ):
        return SimpleNamespace(
            id=message_id,
            author=author,
            channel=channel,
            guild=author.guild if guild is None else guild,
            content=content,
            created_at=created_at
            or datetime.datetime(2026, 8, 2, tzinfo=datetime.UTC),
            type=message_type,
            webhook_id=webhook_id,
        )

    @staticmethod
    def http_error(status=500):
        response = SimpleNamespace(
            status=status,
            reason="Server Error",
            headers={},
        )
        return discord.HTTPException(response, "history failed")

    def test_whole_word_casefold_parser_detects_each_type_once(self):
        from activity_cog import detect_sod_eod

        self.assertEqual(
            detect_sod_eod("SoD, sod and EOD! eod"),
            {"sod", "eod"},
        )
        self.assertEqual(
            detect_sod_eod("sodastream preEoDpost SOD2 3eod"),
            set(),
        )
        self.assertEqual(detect_sod_eod("한글SOD_+EOD한글"), {"sod", "eod"})

    async def test_live_listener_accepts_only_shared_eligible_message_shape(self):
        cog, guild, member = self.make_fixture()
        configured_channel = guild.get_channel(40)
        other_channel = fake_text_channel(41, guild)
        bot_member = FakeMember(2, guild, {10}, bot=True)
        roleless_member = FakeMember(3, guild, set())
        guild.members.extend((bot_member, roleless_member))
        invalid_messages = (
            self.full_message(1, member, configured_channel, guild=None),
            self.full_message(2, member, SimpleNamespace(id=41, guild=guild)),
            self.full_message(3, member, other_channel),
            self.full_message(
                4,
                member,
                configured_channel,
                message_type=discord.MessageType.recipient_add,
            ),
            self.full_message(5, bot_member, configured_channel),
            self.full_message(6, roleless_member, configured_channel),
            self.full_message(7, member, configured_channel, webhook_id=999),
        )
        invalid_messages[0].guild = None

        for message in invalid_messages:
            await cog.on_message(message)

        self.assertEqual(self.count_events(cog), 0)
        await cog.on_message(self.full_message(8, member, configured_channel))
        self.assertEqual(self.count_events(cog), 1)

    async def test_live_both_types_are_kst_deduped_without_advancing_cursor(self):
        cog, guild, member = self.make_fixture()
        channel = guild.get_channel(40)
        created_at = datetime.datetime(2026, 8, 1, 16, 30, tzinfo=datetime.UTC)
        message = self.full_message(
            20,
            member,
            channel,
            "SoD and EOD!",
            created_at=created_at,
        )

        await cog.on_message(message)
        await cog.on_message(message)

        self.assertEqual(self.count_events(cog), 2)
        self.assertEqual(
            await cog._store_call(cog.store.daily_types, 1, 1, "2026-08-02"),
            {"sod", "eod"},
        )
        state = await sync_state(cog, 40)
        self.assertIsNone(state.newest_processed_message_id)
        self.assertIsNone(state.history_from_epoch)

    async def test_live_cache_miss_uses_same_guild_event_member(self):
        cog, guild, member = self.make_fixture()
        guild.members = []

        await cog.on_message(
            self.full_message(22, member, guild.get_channel(40))
        )

        with closing(sqlite3.connect(cog.store.db_path)) as conn:
            stored = conn.execute(
                "SELECT user_id, event_type FROM sod_eod_events"
            ).fetchall()
        self.assertEqual(stored, [(member.id, "sod")])

    async def test_live_rejects_thread_even_when_id_and_parent_match_config(self):
        cog, guild, member = self.make_fixture()
        thread = mock.Mock(spec=discord.Thread)
        thread.id = 40
        thread.guild = guild
        thread.parent = guild.get_channel(40)

        await cog.on_message(self.full_message(23, member, thread))

        self.assertEqual(self.count_events(cog), 0)

    async def test_live_rejects_reply_message_type_explicitly(self):
        cog, guild, member = self.make_fixture()

        await cog.on_message(
            self.full_message(
                24,
                member,
                guild.get_channel(40),
                message_type=discord.MessageType.reply,
            )
        )

        self.assertEqual(self.count_events(cog), 0)

    async def test_live_cache_miss_webhook_is_still_rejected(self):
        cog, guild, member = self.make_fixture()
        guild.members = []

        await cog.on_message(
            self.full_message(
                25,
                member,
                guild.get_channel(40),
                webhook_id=999,
            )
        )

        self.assertEqual(self.count_events(cog), 0)

    async def test_live_store_failure_is_shielded_from_other_bot_features(self):
        cog, guild, member = self.make_fixture()
        error = sqlite3.Error("database unavailable")
        with mock.patch.object(
            cog.store,
            "record_live_message",
            side_effect=error,
        ), mock.patch("activity_cog.logger.exception") as logged:
            await cog.on_message(
                self.full_message(21, member, guild.get_channel(40))
            )

        logged.assert_called_once()

    async def test_backfill_uses_exclusive_after_marker_and_oldest_first(self):
        cog, guild, member = self.make_fixture()
        await prepare_sync_marker(cog, channel_id=40, message_id=11)
        message = self.full_message(12, member, guild.get_channel(40))
        channel = make_history_channel(guild, 40, [message])

        await cog.backfill_current_channel(guild, channel=channel)

        channel.history.assert_called_once_with(
            limit=None,
            oldest_first=True,
            after=discord.Object(id=11),
        )
        self.assertEqual((await sync_state(cog, 40)).newest_processed_message_id, 12)

    async def test_backfill_streams_distinct_authors_with_bounded_lru_eviction(self):
        from activity_cog import AUTHOR_ELIGIBILITY_CACHE_LIMIT

        cog, guild, _member = self.make_fixture()
        guild.members = []
        channel = fake_text_channel(40, guild)
        distinct_total = max(125, AUTHOR_ELIGIBILITY_CACHE_LIMIT + 1)
        author_ids = list(range(1000, 1000 + distinct_total))
        author_sequence = author_ids + [author_ids[-1], author_ids[0]]

        async def fetch_member(user_id):
            return FakeMember(user_id, guild, {10})

        guild.fetch_member = mock.AsyncMock(side_effect=fetch_member)

        async def history(**kwargs):
            for message_id, author_id in enumerate(author_sequence, start=1):
                if message_id > 1:
                    state = cog.store.get_sync_state(guild.id, channel.id)
                    self.assertEqual(
                        state.newest_processed_message_id,
                        message_id - 1,
                        "history was consumed ahead of per-message commit",
                    )
                author = SimpleNamespace(id=author_id, guild=guild, bot=False)
                yield self.full_message(message_id, author, channel)

        channel.history.side_effect = history
        result = await cog.backfill_current_channel(guild, channel=channel)

        self.assertEqual(result.processed_count, len(author_sequence))
        self.assertEqual(result.event_count, len(author_sequence))
        state = await sync_state(cog, 40)
        self.assertEqual(state.newest_processed_message_id, len(author_sequence))
        self.assertIsNotNone(state.completed_epoch)
        fetched_ids = [call.args[0] for call in guild.fetch_member.await_args_list]
        self.assertEqual(fetched_ids.count(author_ids[-1]), 1)
        self.assertEqual(fetched_ids.count(author_ids[0]), 2)
        self.assertEqual(len(fetched_ids), distinct_total + 1)

    async def test_backfill_unresolved_author_is_fetched_once_and_advances_empty(self):
        cog, guild, _member = self.make_fixture()
        channel = fake_text_channel(40, guild)
        author = SimpleNamespace(id=99, bot=False)
        messages = [
            SimpleNamespace(
                id=message_id,
                author=author,
                channel=channel,
                guild=guild,
                content="SoD",
                created_at=datetime.datetime(2026, 8, 2, tzinfo=datetime.UTC),
                type=discord.MessageType.default,
                webhook_id=None,
            )
            for message_id in (12, 13)
        ]
        channel.history.side_effect = lambda **kwargs: self._iterate(messages)
        not_found = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found", headers={}),
            "member",
        )
        guild.fetch_member = mock.AsyncMock(side_effect=not_found)

        result = await cog.backfill_current_channel(guild, channel=channel)

        self.assertEqual(result.processed_count, 2)
        self.assertEqual(result.event_count, 0)
        self.assertEqual(self.count_events(cog), 0)
        self.assertEqual((await sync_state(cog, 40)).newest_processed_message_id, 13)
        guild.fetch_member.assert_awaited_once_with(99)

    @staticmethod
    async def _iterate(items):
        for item in items:
            yield item

    async def test_backfill_fetches_missing_eligible_member_once_and_memoizes(self):
        cog, guild, member = self.make_fixture()
        guild.members = []
        channel = fake_text_channel(40, guild)
        messages = [
            self.full_message(message_id, member, channel)
            for message_id in (30, 31)
        ]
        channel.history.side_effect = lambda **kwargs: self._iterate(messages)
        guild.fetch_member = mock.AsyncMock(return_value=member)

        await cog.backfill_current_channel(guild, channel=channel)

        guild.fetch_member.assert_awaited_once_with(member.id)
        self.assertEqual(self.count_events(cog), 2)

    async def test_backfill_rechecks_cached_member_role_removal_each_message(self):
        cog, guild, member = self.make_fixture()
        channel = fake_text_channel(40, guild)

        async def history(**kwargs):
            yield self.full_message(40, member, channel)
            guild.members = [member.with_roles(set())]
            yield self.full_message(41, member, channel)

        channel.history.side_effect = history

        result = await cog.backfill_current_channel(guild, channel=channel)

        self.assertEqual(result.processed_count, 2)
        self.assertEqual(result.event_count, 1)
        with closing(sqlite3.connect(cog.store.db_path)) as conn:
            message_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT message_id FROM sod_eod_events ORDER BY message_id"
                )
            ]
        self.assertEqual(message_ids, [40])
        self.assertEqual((await sync_state(cog, 40)).newest_processed_message_id, 41)

    async def test_backfill_rechecks_cached_member_role_addition_each_message(self):
        cog, guild, member = self.make_fixture()
        roleless_member = member.with_roles(set())
        guild.members = [roleless_member]
        channel = fake_text_channel(40, guild)

        async def history(**kwargs):
            yield self.full_message(42, roleless_member, channel)
            guild.members = [member]
            yield self.full_message(43, roleless_member, channel)

        channel.history.side_effect = history

        result = await cog.backfill_current_channel(guild, channel=channel)

        self.assertEqual(result.processed_count, 2)
        self.assertEqual(result.event_count, 1)
        with closing(sqlite3.connect(cog.store.db_path)) as conn:
            message_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT message_id FROM sod_eod_events ORDER BY message_id"
                )
            ]
        self.assertEqual(message_ids, [43])
        self.assertEqual((await sync_state(cog, 40)).newest_processed_message_id, 43)

    async def test_backfill_history_interruption_resumes_at_last_committed_message(self):
        cog, guild, member = self.make_fixture()
        channel = fake_text_channel(40, guild)

        async def interrupted_history(**kwargs):
            yield self.full_message(50, member, channel)
            raise self.http_error()

        channel.history.side_effect = interrupted_history
        with self.assertRaises(discord.HTTPException):
            await cog.backfill_current_channel(guild, channel=channel)

        interrupted_state = await sync_state(cog, 40)
        self.assertEqual(interrupted_state.newest_processed_message_id, 50)
        self.assertIsNone(interrupted_state.completed_epoch)

        resumed = make_history_channel(
            guild,
            40,
            [self.full_message(51, member, guild.get_channel(40))],
        )
        await cog.backfill_current_channel(guild, channel=resumed)
        resumed.history.assert_called_once_with(
            limit=None,
            oldest_first=True,
            after=discord.Object(id=50),
        )
        self.assertIsNotNone((await sync_state(cog, 40)).completed_epoch)

    async def test_backfill_fetch_http_error_stops_at_last_committed_message(self):
        cog, guild, member = self.make_fixture()
        guild.members = [member]
        channel = fake_text_channel(40, guild)
        missing_author = SimpleNamespace(id=999, guild=guild, bot=False)
        messages = [
            self.full_message(52, member, channel),
            self.full_message(53, missing_author, channel),
        ]
        channel.history.side_effect = lambda **kwargs: self._iterate(messages)
        guild.fetch_member = mock.AsyncMock(side_effect=self.http_error())

        with self.assertRaises(discord.HTTPException):
            await cog.backfill_current_channel(guild, channel=channel)

        state = await sync_state(cog, 40)
        self.assertEqual(state.newest_processed_message_id, 52)
        self.assertIsNone(state.completed_epoch)

    async def test_backfill_database_interruption_does_not_skip_failed_message(self):
        cog, guild, member = self.make_fixture()
        channel = make_history_channel(
            guild,
            40,
            [
                self.full_message(60, member, guild.get_channel(40)),
                self.full_message(61, member, guild.get_channel(40)),
            ],
        )
        original = cog.store.record_backfill_message_and_advance_cursor

        def fail_second(**kwargs):
            if kwargs["message_id"] == 61:
                raise sqlite3.Error("database unavailable")
            return original(**kwargs)

        with mock.patch.object(
            cog.store,
            "record_backfill_message_and_advance_cursor",
            side_effect=fail_second,
        ), self.assertRaises(sqlite3.Error):
            await cog.backfill_current_channel(guild, channel=channel)

        state = await sync_state(cog, 40)
        self.assertEqual(state.newest_processed_message_id, 60)
        self.assertIsNone(state.completed_epoch)

    async def test_live_overlap_before_backfill_dedupes_and_still_moves_cursor(self):
        cog, guild, member = self.make_fixture()
        await prepare_sync_marker(cog, channel_id=40, message_id=11)
        await record_live_message_for_test(cog, message_id=13, content="SoD")
        self.assertEqual((await sync_state(cog, 40)).newest_processed_message_id, 11)
        channel = make_history_channel(
            guild,
            40,
            [self.full_message(13, member, guild.get_channel(40))],
        )

        await cog.backfill_current_channel(guild, channel=channel)

        self.assertEqual((await sync_state(cog, 40)).newest_processed_message_id, 13)
        self.assertEqual(self.count_events(cog), 1)

    async def test_backfill_holds_existing_guild_lock_before_sod_change(self):
        cog, guild, member = self.make_fixture()
        entered, release = asyncio.Event(), asyncio.Event()
        channel = make_controlled_history_channel(
            guild,
            40,
            [self.full_message(70, member, guild.get_channel(40))],
            entered,
            release,
        )
        backfill = asyncio.create_task(
            cog.backfill_current_channel(guild, channel=channel)
        )
        await entered.wait()
        change = asyncio.create_task(set_sod_channel_for_test(cog, guild, 41, 101))
        await asyncio.sleep(0)

        self.assertFalse(change.done())
        release.set()
        await backfill
        await change
        config = await cog._store_call(cog.store.get_config, guild.id)
        self.assertEqual(config.sod_eod_channel_id, 41)

    async def test_backfill_rejects_wrong_or_inaccessible_current_channel(self):
        for replacement in (
            lambda guild: fake_category(40, guild),
            lambda guild: fake_text_channel(40, guild, can_read=False),
        ):
            with self.subTest(replacement=replacement):
                cog, guild, _member = self.make_fixture()
                guild.channels = [
                    channel for channel in guild.channels if channel.id != 40
                ] + [replacement(guild)]

                with self.assertRaises((ValueError, ChannelChanged)):
                    await cog.backfill_current_channel(guild)

                state = await sync_state(cog, 40)
                self.assertIsNone(state.newest_processed_message_id)
                self.assertIsNone(state.completed_epoch)

    async def test_backfill_command_guards_then_defers_before_lock_and_lookup(self):
        cog, guild, member = self.make_fixture()
        unauthorized = fake_interaction(2, False, guild)
        with mock.patch.object(
            cog,
            "backfill_current_channel",
            new=mock.AsyncMock(),
        ) as backfill:
            await cog.backfill_command.callback(cog, unauthorized)
        backfill.assert_not_awaited()
        self.assertFalse(unauthorized.response.deferred)

        channel = make_history_channel(
            guild,
            40,
            [self.full_message(80, member, guild.get_channel(40))],
        )
        guild.channels = [item for item in guild.channels if item.id != 40] + [channel]
        interaction = fake_deferred_interaction(1, True, guild)
        deferred = asyncio.Event()
        original_defer = interaction.response.defer

        async def observe_defer(**kwargs):
            await original_defer(**kwargs)
            deferred.set()

        interaction.response.defer = observe_defer
        holder_entered, release_holder = asyncio.Event(), asyncio.Event()

        async def hold_lock():
            async with cog.guild_locks[guild.id]:
                holder_entered.set()
                await release_holder.wait()

        holder = asyncio.create_task(hold_lock())
        await holder_entered.wait()
        command = asyncio.create_task(cog.backfill_command.callback(cog, interaction))
        await asyncio.wait_for(deferred.wait(), 1)

        self.assertTrue(interaction.response.defer_kwargs["ephemeral"])
        channel.history.assert_not_called()
        release_holder.set()
        await asyncio.gather(holder, command)
        self.assertEqual(len(interaction.original_edits), 1)
        self.assertEqual(interaction.followup.sent, [])

    async def test_backfill_command_error_completes_deferred_response_once(self):
        cog, guild, _member = self.make_fixture()
        interaction = fake_deferred_interaction(1, True, guild)
        terminal_edit = mock.AsyncMock(wraps=cog._complete_ephemeral)
        with mock.patch.object(
            cog,
            "backfill_current_channel",
            new=mock.AsyncMock(side_effect=sqlite3.Error("database unavailable")),
        ), mock.patch.object(
            cog,
            "_complete_ephemeral",
            new=terminal_edit,
        ), mock.patch("activity_cog.logger.exception"):
            await cog.backfill_command.callback(cog, interaction)

        self.assertTrue(interaction.response.deferred)
        terminal_edit.assert_awaited_once()
        self.assertEqual(len(interaction.original_edits), 1)
        self.assertIn("실패", interaction.original_edits[0]["content"])
        self.assertEqual(interaction.followup.sent, [])

    async def test_backfill_success_terminal_edit_failure_is_never_retried(self):
        from activity_cog import BackfillResult

        cog, guild, _member = self.make_fixture()
        interaction = fake_deferred_interaction(1, True, guild)
        terminal_edit = mock.AsyncMock(side_effect=self.http_error())
        with mock.patch.object(
            cog,
            "backfill_current_channel",
            new=mock.AsyncMock(return_value=BackfillResult(3, 2)),
        ), mock.patch.object(
            cog,
            "_complete_ephemeral",
            new=terminal_edit,
        ), mock.patch("activity_cog.logger.exception") as logged:
            with self.assertRaises(discord.HTTPException):
                await cog.backfill_command.callback(cog, interaction)

        terminal_edit.assert_awaited_once()
        content = terminal_edit.await_args.args[1]
        self.assertIn("감지 2개", content)
        self.assertNotIn("기록 2개", content)
        logged.assert_called_once()

    def test_only_message_create_listener_is_registered_for_text_activity(self):
        from activity_cog import ActivityCog

        listener_names = {name for name, _method_name in ActivityCog.__cog_listeners__}
        self.assertIn("on_message", listener_names)
        self.assertNotIn("on_message_edit", listener_names)
        self.assertNotIn("on_message_delete", listener_names)


class ActivityReportViewTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def row(index=1, *, display_name="Member", last_activity_epoch=100):
        return ReportRow(
            user_id=index,
            display_name=display_name,
            last_activity_epoch=last_activity_epoch,
            reading_seconds=61,
            study_seconds=122,
            reading_session_count=2,
            study_session_count=3,
            sod_days=4,
            eod_days=5,
            combined_days=6,
        )

    @staticmethod
    def make_view(report, *, owner_id=1, guild_id=1):
        from activity_cog import ActivityReportView

        original = fake_deferred_interaction(owner_id, True, FakeGuild(guild_id))
        return (
            ActivityReportView(
                owner_id=owner_id,
                guild_id=guild_id,
                report=report,
                original_response_editor=original.edit_original_response,
            ),
            original,
        )

    def test_exact_page_counts_and_all_rows_are_reachable(self):
        from activity_cog import format_report_page

        for count, expected_pages in ((1, 1), (15, 1), (16, 2), (55, 4)):
            with self.subTest(count=count):
                report = report_with_members(count)
                self.assertEqual(report.page_count, expected_pages)
                pages = [format_report_page(report, page) for page in range(expected_pages)]
                for member_id in range(count):
                    self.assertTrue(
                        any(f"[{member_id}]" in page for page in pages),
                        f"member {member_id} was not reachable",
                    )

    async def test_three_argument_constructor_binds_first_authorized_guild(self):
        from activity_cog import ActivityReportView

        original = fake_interaction(1, True, FakeGuild(1))
        view = ActivityReportView(
            1,
            report_with_members(16),
            original.edit_original_response,
        )
        first = fake_interaction(1, True, FakeGuild(1))
        await press(view, view.next_page, first)
        self.assertEqual(view.guild_id, 1)

        other_guild = fake_interaction(1, True, FakeGuild(2))
        await press(view, view.previous_page, other_guild)
        self.assertEqual(view.page_index, 1)
        self.assertTrue(other_guild.response.sent[0][1]["ephemeral"])

    async def test_page_buttons_clamp_and_round_trip_for_55_members(self):
        report = report_with_members(55)
        view, _original = self.make_view(report)
        interaction = fake_interaction(1, True, FakeGuild(1))

        for _ in range(9):
            await press(view, view.next_page, interaction)
        self.assertEqual(view.page_index, 3)
        self.assertTrue(view.next_page.disabled)
        self.assertFalse(view.previous_page.disabled)

        for _ in range(9):
            await press(view, view.previous_page, interaction)
        self.assertEqual(view.page_index, 0)
        self.assertTrue(view.previous_page.disabled)
        self.assertFalse(view.next_page.disabled)

    def test_empty_page_and_long_unicode_names_stay_below_content_limit(self):
        from activity_cog import format_report_page

        empty = format_report_page(make_report([]), 999)
        self.assertIn("표시할 대상 멤버가 없습니다", empty)

        long_name = "👩🏽‍💻한글𠮷" * 300
        report = make_report(
            [self.row(index, display_name=long_name) for index in range(1, 16)]
        )
        page = format_report_page(report, 0)
        self.assertLessEqual(len(page), 1900)
        page.encode("utf-8")
        self.assertIn("[1]", page)
        self.assertIn("[15]", page)

    def test_worst_case_page_keeps_every_row_and_field_complete(self):
        from activity_cog import format_report_page

        huge = 10**250
        rows = [
            ReportRow(
                user_id=index,
                display_name=f"N{index:02d}-" + ("👩🏽‍💻한글𠮷" * 200),
                last_activity_epoch=huge,
                reading_seconds=huge,
                study_seconds=huge - index,
                reading_session_count=huge - 100,
                study_session_count=huge - 200,
                sod_days=huge - 300,
                eod_days=huge - 400,
                combined_days=huge - 500,
            )
            for index in range(1, 16)
        ]
        warnings = [
            CoverageWarning(
                code=f"warning_{index:02d}_" + ("x" * 100),
                text=f"경고 {index:02d} " + ("매우 긴 경고 내용 " * 100),
            )
            for index in range(30)
        ]

        page = format_report_page(make_report(rows, warnings=warnings), 0)
        row_lines = [line for line in page.splitlines() if line.startswith("[")]

        self.assertLessEqual(len(page), 1900)
        page.encode("utf-8")
        self.assertEqual(len(row_lines), 15)
        for index, line in enumerate(row_lines, 1):
            self.assertIn(f"[{index}]", line)
            self.assertIn(f"N{index:02d}", line)
            for label in (
                "최근=",
                "독서초=",
                "독서회=",
                "스터디초=",
                "스터디회=",
                "SoD=",
                "EoD=",
                "통합=",
            ):
                self.assertIn(label, line)
        self.assertIn("전체 TXT", page)

    def test_page_and_txt_share_warning_text(self):
        from activity_cog import build_report_txt, format_report_page

        warning = CoverageWarning(
            code="gateway_disconnect",
            text="음성 수집 공백: 160~200",
        )
        report = make_report([self.row()], warnings=[warning])
        page = format_report_page(report, 0)
        text = build_report_txt(report)
        self.assertIn(warning.text, page)
        self.assertIn(warning.text, text)

    async def test_owner_admin_and_same_guild_are_rechecked_before_any_mutation(self):
        report = report_with_members(16)
        view, original = self.make_view(report)
        original_editor = view.original_response_editor
        attempts = (
            (2, True, 1),
            (1, False, 1),
            (1, True, 2),
        )

        for user_id, administrator, guild_id in attempts:
            with self.subTest(user=user_id, guild=guild_id):
                page_interaction = fake_interaction(
                    user_id, administrator, FakeGuild(guild_id)
                )
                await press(view, view.next_page, page_interaction)
                self.assertEqual(view.page_index, 0)
                self.assertEqual(page_interaction.response.edits, [])
                self.assertTrue(page_interaction.response.sent)
                self.assertTrue(
                    page_interaction.response.sent[-1][1]["ephemeral"]
                )

                txt_interaction = fake_interaction(
                    user_id, administrator, FakeGuild(guild_id)
                )
                await press(view, view.full_txt_button, txt_interaction)
                self.assertEqual(txt_interaction.followup.sent, [])
                self.assertTrue(txt_interaction.response.sent)
                self.assertIs(view.original_response_editor, original_editor)
        self.assertEqual(original.original_edits, [])

    async def test_denied_click_does_not_replace_latest_authorized_editor(self):
        view, _original = self.make_view(report_with_members(16))
        authorized = fake_interaction(1, True, FakeGuild(1))
        await press(view, view.next_page, authorized)
        authorized_editor = view.original_response_editor

        denied = fake_interaction(2, True, FakeGuild(1))
        await press(view, view.previous_page, denied)

        self.assertIs(view.last_authorized_interaction, authorized)
        self.assertIs(view.original_response_editor, authorized_editor)
        self.assertEqual(view.page_index, 1)
        self.assertTrue(denied.response.sent[0][1]["ephemeral"])

    async def test_accepted_click_updates_latest_editor_used_by_timeout(self):
        report = report_with_members(16)
        view, first = self.make_view(report)
        latest = fake_interaction(1, True, FakeGuild(1))

        await press(view, view.next_page, latest)
        await view.on_timeout()

        self.assertEqual(first.original_edits, [])
        self.assertEqual(latest.original_edits[-1]["view"], view)
        self.assertTrue(all(child.disabled for child in view.children))
        self.assertLessEqual(view.timeout, 600)

    async def test_txt_click_near_ten_minutes_refreshes_report_editor_for_timeout(self):
        report = report_with_members(1)
        view, initial_interaction = self.make_view(report)
        initial_interaction.edit_original_response = mock.AsyncMock(
            side_effect=discord.DiscordException("initial token expired")
        )
        view.original_response_editor = initial_interaction.edit_original_response
        txt_interaction = fake_interaction(1, True, FakeGuild(1))

        await press(view, view.full_txt_button, txt_interaction)
        await view.on_timeout()

        self.assertIs(view.last_authorized_interaction, txt_interaction)
        self.assertIs(
            view.original_response_editor,
            txt_interaction.edit_original_response,
        )
        initial_interaction.edit_original_response.assert_not_awaited()
        self.assertEqual(txt_interaction.original_edits[-1]["view"], view)
        self.assertTrue(all(child.disabled for child in view.children))

    async def test_timeout_edit_failure_only_logs(self):
        report = report_with_members(1)
        view, _original = self.make_view(report)
        view.original_response_editor = mock.AsyncMock(
            side_effect=discord.DiscordException("expired")
        )
        with mock.patch("activity_cog.logger.warning") as logged:
            await view.on_timeout()
        logged.assert_called_once()
        view.original_response_editor.assert_awaited_once_with(view=view)

    async def test_txt_defers_then_sends_one_use_bytesio_file_with_all_fields(self):
        from activity_cog import build_report_txt

        report = make_report([self.row(display_name="보고 대상")])
        view, _original = self.make_view(report)
        interaction = fake_interaction(1, True, FakeGuild(1))

        await press(view, view.full_txt_button, interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertFalse(interaction.response.defer_kwargs["thinking"])
        self.assertEqual(len(interaction.followup.sent), 1)
        _content, kwargs = interaction.followup.sent[0]
        self.assertTrue(kwargs["ephemeral"])
        attachment = kwargs["file"]
        self.assertIsInstance(attachment, discord.File)
        self.assertEqual(attachment.filename, report.txt_filename)
        self.assertIsInstance(attachment.fp, __import__("io").BytesIO)
        payload = attachment.fp.getvalue().decode("utf-8")
        self.assertEqual(payload, build_report_txt(report))
        for expected in (
            "보고 대상",
            "user_id=1",
            "last_activity_utc=",
            "last_activity_kst=",
            "reading_seconds=61",
            "reading_session_count=2",
            "study_seconds=122",
            "study_session_count=3",
            "sod_days=4",
            "eod_days=5",
            "combined_days=6",
            report.period_label,
            "생성 UTC:",
            "생성 KST:",
        ):
            self.assertIn(expected, payload)


class ActivityReportCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_fixture(self):
        cog, guild, member = configured_fixture()
        self.addCleanup(cog._test_tmp.cleanup)
        return cog, guild, member

    @staticmethod
    async def invoke(command, cog, interaction, *args, now=100):
        with mock.patch("activity_cog.utc_now_epoch", return_value=now) as clock:
            await command.callback(cog, interaction, *args)
        return clock

    def test_report_group_is_guild_only_with_admin_registration_hint_and_range(self):
        from activity_cog import ActivityCog

        self.assertTrue(ActivityCog.report_group.guild_only)
        self.assertTrue(ActivityCog.report_group.default_permissions.administrator)
        recent_parameter = ActivityCog.recent_report.parameters[0]
        self.assertEqual(recent_parameter.name, "일수")
        self.assertEqual(recent_parameter.min_value, 1)

    async def test_non_admin_is_rejected_before_defer_and_store(self):
        cog, guild, _member = self.make_fixture()
        interaction = fake_interaction(2, False, guild)
        with mock.patch.object(cog, "_store_call", new=mock.AsyncMock()) as store_call:
            await cog.recent_report.callback(cog, interaction, 1)
        store_call.assert_not_awaited()
        self.assertFalse(interaction.response.deferred)
        self.assertTrue(interaction.response.sent[0][1]["ephemeral"])

    async def test_recent_uses_inclusive_kst_today_and_captures_generation_once(self):
        cog, guild, _member = self.make_fixture()
        generated = int(
            datetime.datetime(2024, 3, 1, 0, 30, tzinfo=KST).timestamp()
        )
        interaction = fake_interaction(1, True, guild)

        clock = await self.invoke(
            cog.recent_report,
            cog,
            interaction,
            2,
            now=generated,
        )

        self.assertTrue(interaction.response.deferred)
        self.assertTrue(interaction.response.defer_kwargs["ephemeral"])
        self.assertEqual(len(interaction.original_edits), 1)
        report = interaction.original_edits[0]["view"].report
        self.assertEqual(report.start_date, datetime.date(2024, 2, 29))
        self.assertEqual(report.end_date, datetime.date(2024, 3, 1))
        self.assertEqual(report.generated_epoch, generated)
        clock.assert_called_once_with()

    async def test_admin_defer_precedes_period_validation_and_first_report_lookup(self):
        cog, guild, _member = self.make_fixture()
        interaction = fake_interaction(1, True, guild)
        original = cog._invalidate_configured_resources

        async def observe_lookup(*args, **kwargs):
            self.assertTrue(interaction.response.deferred)
            self.assertTrue(interaction.response.defer_kwargs["ephemeral"])
            return await original(*args, **kwargs)

        with mock.patch.object(
            cog,
            "_invalidate_configured_resources",
            side_effect=observe_lookup,
        ) as lookup:
            await self.invoke(cog.recent_report, cog, interaction, 1)
        lookup.assert_awaited_once()

        invalid = fake_interaction(1, True, guild)
        await self.invoke(cog.recent_report, cog, invalid, 0)
        self.assertTrue(invalid.response.deferred)
        self.assertEqual(len(invalid.original_edits), 1)
        self.assertIn("1 이상", invalid.original_edits[0]["content"])

    async def test_explicit_leap_date_is_strict_inclusive_and_start_must_not_follow_end(self):
        cog, guild, _member = self.make_fixture()
        valid = fake_interaction(1, True, guild)
        await self.invoke(
            cog.period_report,
            cog,
            valid,
            "2024-02-29",
            "2024-03-01",
        )
        report = valid.original_edits[0]["view"].report
        self.assertEqual(
            (report.start_epoch, report.end_epoch),
            kst_range_to_epoch(datetime.date(2024, 2, 29), datetime.date(2024, 3, 1)),
        )

        for start, end in (
            ("2024-2-29", "2024-03-01"),
            ("2023-02-29", "2024-03-01"),
            ("2024-03-02", "2024-03-01"),
        ):
            with self.subTest(start=start, end=end):
                interaction = fake_interaction(1, True, guild)
                await self.invoke(cog.period_report, cog, interaction, start, end)
                self.assertEqual(len(interaction.original_edits), 1)
                self.assertNotIn("view", interaction.original_edits[0])
                self.assertTrue(
                    "YYYY-MM-DD" in interaction.original_edits[0]["content"]
                    or "늦을 수 없습니다" in interaction.original_edits[0]["content"]
                )

    async def test_current_eligible_nonbot_members_include_zero_records_only(self):
        cog, guild, member = self.make_fixture()
        guild.members = [
            member.with_roles({10}),
            FakeMember(2, guild, {10}, display_name="Zero"),
            FakeMember(3, guild, set(), display_name="Roleless"),
            FakeMember(4, guild, {10}, bot=True, display_name="Bot"),
        ]
        interaction = fake_interaction(1, True, guild)

        await self.invoke(cog.recent_report, cog, interaction, 1)

        rows = interaction.original_edits[0]["view"].report.rows
        self.assertEqual({row.user_id for row in rows}, {1, 2})
        zero = next(row for row in rows if row.user_id == 2)
        self.assertIsNone(zero.last_activity_epoch)
        self.assertEqual(zero.reading_seconds, 0)
        self.assertEqual(zero.combined_days, 0)

    async def test_open_session_last_activity_uses_same_single_captured_epoch(self):
        cog, guild, member = self.make_fixture()
        await cog._store_call(
            cog.store.reconcile_session,
            guild.id,
            member.id,
            "reading_room",
            10,
        )
        generated = 200
        interaction = fake_interaction(1, True, guild)

        clock = await self.invoke(
            cog.recent_report,
            cog,
            interaction,
            1,
            now=generated,
        )

        report = interaction.original_edits[0]["view"].report
        self.assertEqual(report.generated_epoch, generated)
        self.assertEqual(report.rows[0].last_activity_epoch, generated)
        clock.assert_called_once_with()

    async def test_incomplete_config_produces_one_useful_terminal_edit(self):
        from activity_cog import ActivityCog

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = ActivityStore(str(Path(temp_dir.name) / "activity.db"))
        store.initialize()
        guild = FakeGuild(1)
        cog = ActivityCog(FakeBot(), store)
        interaction = fake_interaction(1, True, guild)

        await self.invoke(cog.recent_report, cog, interaction, 1)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(interaction.original_edits), 1)
        self.assertIn("설정", interaction.original_edits[0]["content"])
        self.assertEqual(interaction.followup.sent, [])

    async def test_build_error_and_terminal_edit_error_each_attempt_one_terminal_edit(self):
        cog, guild, _member = self.make_fixture()
        build_error = fake_interaction(1, True, guild)
        with mock.patch.object(
            cog.store,
            "build_report",
            side_effect=sqlite3.Error("database unavailable"),
        ), mock.patch("activity_cog.logger.exception"):
            await self.invoke(cog.recent_report, cog, build_error, 1)
        self.assertEqual(len(build_error.original_edits), 1)
        self.assertIn("만들지 못했습니다", build_error.original_edits[0]["content"])
        self.assertEqual(build_error.followup.sent, [])

        terminal_error = fake_interaction(1, True, guild)
        terminal_error.edit_original_response = mock.AsyncMock(
            side_effect=discord.DiscordException("expired")
        )
        with mock.patch("activity_cog.logger.exception") as logged:
            await self.invoke(cog.recent_report, cog, terminal_error, 1)
        terminal_error.edit_original_response.assert_awaited_once()
        self.assertEqual(terminal_error.followup.sent, [])
        logged.assert_called_once()
