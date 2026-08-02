import asyncio
import os
from collections import defaultdict

from discord.ext import commands

from activity_store import ActivityStore


class ActivityCog(commands.Cog):
    def __init__(self, bot, store):
        self.bot = bot
        self.store = store
        self.store_lock = asyncio.Lock()
        self.guild_locks = defaultdict(asyncio.Lock)
        self.collection_gates = defaultdict(asyncio.Event)

    async def _store_call(self, method, *args, **kwargs):
        async with self.store_lock:
            return await asyncio.to_thread(method, *args, **kwargs)


async def setup(bot):
    store = ActivityStore(os.getenv("ACTIVITY_DB_PATH", "/data/activity.db"))
    await asyncio.to_thread(store.initialize)
    await bot.add_cog(ActivityCog(bot, store))
