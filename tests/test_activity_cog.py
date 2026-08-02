import asyncio
import os
import sqlite3
import threading
import unittest
from unittest import mock

import bot as bot_module

from tests.activity_fixtures import FakeBot


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
        entered = []
        first_entered = threading.Event()
        release_first = threading.Event()

        def recording_method(name):
            entered.append(name)
            if name == "first":
                first_entered.set()
                release_first.wait(timeout=1)
            return name

        first = asyncio.create_task(self.cog._store_call(recording_method, "first"))
        reached_worker = await asyncio.to_thread(first_entered.wait, 1)
        self.assertTrue(reached_worker)
        second = asyncio.create_task(self.cog._store_call(recording_method, "second"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(entered, ["first"])

        release_first.set()
        self.assertEqual(await asyncio.gather(first, second), ["first", "second"])
        self.assertEqual(entered, ["first", "second"])

    def test_cog_has_shared_store_lock_and_per_guild_locks_and_gates(self):
        self.assertIs(self.cog.guild_locks[1], self.cog.guild_locks[1])
        self.assertIsNot(self.cog.guild_locks[1], self.cog.guild_locks[2])
        self.assertIs(self.cog.collection_gates[1], self.cog.collection_gates[1])
        self.assertIsNot(self.cog.collection_gates[1], self.cog.collection_gates[2])
        self.assertFalse(self.cog.collection_gates[1].is_set())
