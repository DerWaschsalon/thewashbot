"""Mitglieder-Zähler: Name eines Sprachkanals zeigt die aktuelle Mitgliederzahl. Discord erlaubt
Namensänderungen an einem Kanal nur begrenzt oft (grob 2 je 10 Minuten) — deshalb höchstens alle
10 Minuten automatisch aktualisieren, dazu einmal sofort bei der Einrichtung.
"""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

UPDATE_INTERVAL_SECONDS = 10 * 60


def channel_name(guild: discord.Guild) -> str:
    return f"👥 Mitglieder: {guild.member_count}"


class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.refresh.start()

    def cog_unload(self):
        self.refresh.cancel()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._maybe_update(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self._maybe_update(member.guild)

    async def _maybe_update(self, guild: discord.Guild) -> None:
        last = self.db.get(guild.id, "member_count_last")
        if last and time.time() - float(last) < UPDATE_INTERVAL_SECONDS:
            return
        await self.update_guild(guild)

    async def update_guild(self, guild: discord.Guild) -> None:
        channel_id = self.db.get_int(guild.id, "member_count_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not channel:
            return
        name = channel_name(guild)
        if channel.name == name:
            return
        try:
            await channel.edit(name=name, reason="Mitglieder-Zähler")
        except discord.HTTPException as ex:
            log.warning("Mitglieder-Zähler %s: %r", guild.id, ex)
            return
        await self.db.set(guild.id, "member_count_last", time.time())

    @tasks.loop(minutes=10)
    async def refresh(self):
        for guild_id in self.db.guilds_with("member_count_channel"):
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self.update_guild(guild)

    @refresh.before_loop
    async def before_refresh(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Stats(bot))
