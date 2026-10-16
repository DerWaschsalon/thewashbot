"""Bump-Erinnerung: erkennt den Bump-Bestätigungs-Post von DISBOARD im eingerichteten Kanal
und erinnert nach Ablauf des 2-Stunden-Cooldowns per Ping im selben Kanal. Rein passiv — der Bot
bumpt nicht selbst (das wäre automatisiertes Verhalten und ein möglicher Verstoß gegen Disboards
eigene Regeln), er erinnert nur daran, dass `/bump` wieder getippt werden kann.
"""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

DISBOARD_BOT_ID = 302050872383242240
BUMP_INTERVAL_SECONDS = 2 * 60 * 60


def is_bump_confirmation(message: discord.Message) -> bool:
    """True, wenn diese DISBOARD-Nachricht eine erfolgreiche Bump-Bestätigung ist — nicht die
    Cooldown-Meldung, die DISBOARD bei zu frühem erneutem `/bump` schickt."""
    if message.author.id != DISBOARD_BOT_ID or not message.embeds:
        return False
    embed = message.embeds[0]
    text = " ".join(filter(None, [embed.title, embed.description])).lower()
    return "bump done" in text or "bump erfolgreich" in text


class Bump(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.check_due.start()

    def cog_unload(self):
        self.check_due.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.id != DISBOARD_BOT_ID:
            return
        channel_id = self.db.get_int(message.guild.id, "bump_channel")
        if channel_id != message.channel.id or not is_bump_confirmation(message):
            return
        await self.db.set(message.guild.id, "bump_last", time.time())
        await self.db.set(message.guild.id, "bump_reminded", False)

    @tasks.loop(minutes=1)
    async def check_due(self):
        now = time.time()
        for guild_id in self.db.guilds_with("bump_channel"):
            last = self.db.get(guild_id, "bump_last")
            if not last or self.db.get_bool(guild_id, "bump_reminded"):
                continue
            if now - float(last) < BUMP_INTERVAL_SECONDS:
                continue
            await self._remind(guild_id)
            await self.db.set(guild_id, "bump_reminded", True)

    @check_due.before_loop
    async def before_check_due(self):
        await self.bot.wait_until_ready()

    async def _remind(self, guild_id: int) -> None:
        channel_id = self.db.get_int(guild_id, "bump_channel")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not channel:
            return
        role_id = self.db.get_int(guild_id, "bump_role")
        role = channel.guild.get_role(role_id) if role_id else None
        text = f"{role.mention} ⏰ Bump ist wieder möglich – wer mag, tippt `/bump`!" if role else \
            "⏰ Bump ist wieder möglich – wer mag, tippt `/bump`!"
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(roles=bool(role)))
        except discord.HTTPException as ex:
            log.warning("Bump-Erinnerung %s: %r", guild_id, ex)


async def setup(bot):
    await bot.add_cog(Bump(bot))
