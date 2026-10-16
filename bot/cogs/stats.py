"""Statistik-Kanäle: Namen von Sprachkanälen zeigen laufend Mitglieder- bzw. Bot-Anzahl. Discord
erlaubt Namensänderungen an einem Kanal nur begrenzt oft (grob 2 je 10 Minuten) — deshalb pro
Kanal höchstens alle 10 Minuten automatisch aktualisieren, dazu einmal sofort bei der Einrichtung.
"""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

UPDATE_INTERVAL_SECONDS = 10 * 60


def member_channel_name(guild: discord.Guild) -> str:
    return f"👥 Mitglieder: {guild.member_count}"


def bots_channel_name(guild: discord.Guild) -> str:
    return f"🤖 Bots: {sum(1 for m in guild.members if m.bot)}"


# (Einstellungs-Schlüssel für den Kanal, Schlüssel für den letzten Update-Zeitpunkt, Namensfunktion)
COUNTERS = [
    ("member_count_channel", "member_count_last", member_channel_name),
    ("bots_count_channel", "bots_count_last", bots_channel_name),
]


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
        for channel_key, last_key, _name_fn in COUNTERS:
            last = self.db.get(guild.id, last_key)
            if last and time.time() - float(last) < UPDATE_INTERVAL_SECONDS:
                continue
            await self._update_one(guild, channel_key, last_key, _name_fn)

    async def update_guild(self, guild: discord.Guild) -> None:
        """Aktualisiert alle für diesen Server eingerichteten Zähler sofort (z. B. direkt nach
        der Einrichtung im Panel), unabhängig von der 10-Minuten-Drosselung."""
        for channel_key, last_key, name_fn in COUNTERS:
            await self._update_one(guild, channel_key, last_key, name_fn)

    async def _update_one(self, guild: discord.Guild, channel_key: str, last_key: str, name_fn) -> None:
        channel_id = self.db.get_int(guild.id, channel_key)
        channel = guild.get_channel(channel_id) if channel_id else None
        if not channel:
            return
        name = name_fn(guild)
        if channel.name == name:
            return
        try:
            await channel.edit(name=name, reason="Statistik-Kanal")
        except discord.HTTPException as ex:
            log.warning("Statistik-Kanal %s (%s): %r", guild.id, channel_key, ex)
            return
        await self.db.set(guild.id, last_key, time.time())

    @tasks.loop(minutes=10)
    async def refresh(self):
        guild_ids = {gid for key, _, _ in COUNTERS for gid in self.db.guilds_with(key)}
        for guild_id in guild_ids:
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self.update_guild(guild)

    @refresh.before_loop
    async def before_refresh(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Stats(bot))
