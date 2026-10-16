"""Voice-Statistik: Zeit im Sprachkanal läuft in `levels.voice_seconds` mit — nur wenn nicht
allein im Kanal (mind. 2 Nicht-Bots), nicht im AFK-Kanal, und nicht selbst stummgeschaltet/taub.
Optional zusätzlich XP pro Minute (`voice_xp_per_minute`), für ein künftiges Levelsystem, das auf
derselben `levels`-Tabelle aufbaut.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands, tasks

TICK_SECONDS = 60


def qualifies(member: discord.Member, channel: discord.VoiceChannel) -> bool:
    if member.bot or channel == channel.guild.afk_channel:
        return False
    non_bots = [m for m in channel.members if not m.bot]
    if len(non_bots) < 2:
        return False
    voice = member.voice
    if voice and (voice.self_deaf or voice.deaf):
        return False
    return True


def format_duration(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours} Std. {minutes} Min."
    return f"{minutes} Min."


class VoiceStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    @tasks.loop(seconds=TICK_SECONDS)
    async def tick(self):
        for guild in self.bot.guilds:
            xp_per_minute = guild_voice_xp(self.db, guild.id)
            for channel in guild.voice_channels:
                for member in channel.members:
                    if qualifies(member, channel):
                        await self._add(guild.id, member.id, xp_per_minute)

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()

    async def _add(self, guild_id: int, user_id: int, xp_per_minute: int) -> None:
        await self.db.execute(
            "INSERT INTO levels (guild_id, user_id, voice_seconds, xp) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "voice_seconds=voice_seconds+excluded.voice_seconds, xp=xp+excluded.xp",
            (guild_id, user_id, TICK_SECONDS, xp_per_minute),
        )

    @app_commands.command(name="sprachzeit", description="Verbrachte Zeit im Sprachkanal anzeigen (leer = deine eigene)")
    @app_commands.guild_only()
    async def voice_time(self, interaction: discord.Interaction, nutzer: discord.Member | None = None):
        member = nutzer or interaction.user
        row = await self.db.fetchone(
            "SELECT voice_seconds FROM levels WHERE guild_id=? AND user_id=?", (interaction.guild_id, member.id)
        )
        seconds = row["voice_seconds"] if row else 0
        await interaction.response.send_message(
            f"{member.mention} war {format_duration(seconds)} im Sprachkanal.", ephemeral=True
        )


def guild_voice_xp(db, guild_id: int) -> int:
    return db.get_int(guild_id, "voice_xp_per_minute") or 0


async def setup(bot):
    await bot.add_cog(VoiceStats(bot))
