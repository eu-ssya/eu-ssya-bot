"""모임통장(Wallet) Cog — 스터디 커뮤니티 운영비 관리."""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

logger = logging.getLogger("eu_ssya_bot")


class WalletCog(commands.Cog):
    """모임통장 명령 + UI + rename worker."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WalletCog(bot))
