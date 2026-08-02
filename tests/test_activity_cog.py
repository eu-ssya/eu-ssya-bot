import asyncio
import itertools
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import discord

import bot as bot_module
from activity_store import ActivityStore

from tests.activity_fixtures import (
    FakeBot,
    FakeGuild,
    FakeMember,
    configured_fixture,
    fake_category,
    fake_interaction,
    fake_role,
    fake_text_channel,
    session_rows,
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
            [("reading_room", 50, 100, "reconciled")],
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

    async def test_member_remove_closes_open_session_even_with_closed_gate(self):
        cog, guild, member = self.make_cog()
        await cog.reconcile_member(member, 100)
        cog.collection_gates[guild.id].clear()

        await self.call_at(150, cog.on_member_remove, member)

        self.assertEqual(
            await session_rows(cog, member.id),
            [("reading_room", 100, 150, "reconciled")],
        )

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
