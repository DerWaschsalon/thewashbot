"""Erinnerungen: /erinnere, /erinnerungen, /erinnerung-loeschen.

Zeitangaben: relativ ("2h", "30m", "1d2h") oder absolut ("18:30" heute/morgen,
"24.12. 18:00" mit optionalem Jahr). Alles in Europe/Berlin. Ein Hintergrund-Task prüft alle
30 Sekunden auf fällige Erinnerungen.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..errors import UserError

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Berlin")

RELATIVE_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
DATE_TIME_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(?:(\d{4}))?\s+(\d{1,2}):(\d{2})$")


def parse_when(text: str, now: datetime) -> datetime:
    text = text.strip()
    m = RELATIVE_RE.match(text)
    if m and any(m.groups()):
        days, hours, minutes = (int(g) if g else 0 for g in m.groups())
        return now + timedelta(days=days, hours=hours, minutes=minutes)

    m = TIME_RE.match(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        return when

    m = DATE_TIME_RE.match(text)
    if m:
        day, month, year, hour, minute = m.groups()
        y = int(year) if year else now.year
        try:
            when = datetime(y, int(month), int(day), int(hour), int(minute), tzinfo=TZ)
        except ValueError:
            raise UserError(f"„{text}“ ist kein gültiges Datum.")
        if not year and when <= now:
            when = when.replace(year=y + 1)
        return when

    raise UserError(f"„{text}“ verstehe ich nicht. Beispiele: `2h`, `30m`, `1d2h`, `18:30`, `24.12. 18:00`.")


class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.check_due.start()

    def cog_unload(self):
        self.check_due.cancel()

    @tasks.loop(seconds=30)
    async def check_due(self):
        rows = await self.db.fetchall("SELECT * FROM reminders WHERE due <= ?", (time.time(),))
        for r in rows:
            await self._send(r)
            await self.db.execute("DELETE FROM reminders WHERE id=?", (r["id"],))

    @check_due.before_loop
    async def before_check_due(self):
        await self.bot.wait_until_ready()

    async def _send(self, row) -> None:
        try:
            user = self.bot.get_user(row["user_id"]) or await self.bot.fetch_user(row["user_id"])
        except discord.HTTPException:
            return
        text = f"⏰ Erinnerung: {row['text']}"
        if row["private"]:
            try:
                await user.send(text)
            except discord.HTTPException:
                pass
            return
        channel = self.bot.get_channel(row["channel_id"])
        if channel:
            try:
                await channel.send(f"{user.mention} {text}", allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass

    @app_commands.command(name="erinnere", description="Erinnerung setzen")
    @app_commands.describe(
        wann="z. B. 2h, 30m, 1d2h, 18:30, 24.12. 18:00",
        text="Woran erinnert werden soll",
        privat="Per DM statt im Kanal (Standard: aus)",
    )
    @app_commands.guild_only()
    async def remind(self, interaction: discord.Interaction, wann: str, text: str, privat: bool = False):
        due = parse_when(wann, datetime.now(TZ))
        await self.db.execute(
            "INSERT INTO reminders (guild_id, channel_id, user_id, text, due, private) VALUES (?, ?, ?, ?, ?, ?)",
            (interaction.guild_id, interaction.channel_id, interaction.user.id, text, due.timestamp(), int(privat)),
        )
        await interaction.response.send_message(
            f"Erinnere dich {discord.utils.format_dt(due, 'R')} ({discord.utils.format_dt(due, 'f')})"
            f"{' per DM' if privat else ''}: {text}", ephemeral=True,
        )

    @app_commands.command(name="erinnerungen", description="Deine offenen Erinnerungen anzeigen")
    @app_commands.guild_only()
    async def list_reminders(self, interaction: discord.Interaction):
        rows = await self.db.fetchall(
            "SELECT id, text, due FROM reminders WHERE guild_id=? AND user_id=? ORDER BY due",
            (interaction.guild_id, interaction.user.id),
        )
        if not rows:
            return await interaction.response.send_message("Du hast keine offenen Erinnerungen.", ephemeral=True)
        lines = [
            f"**#{r['id']}** {discord.utils.format_dt(datetime.fromtimestamp(r['due'], tz=TZ), 'R')} – {r['text']}"
            for r in rows
        ]
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @app_commands.command(name="erinnerung-loeschen", description="Eigene Erinnerung per ID löschen (siehe /erinnerungen)")
    @app_commands.guild_only()
    async def delete_reminder(self, interaction: discord.Interaction, erinnerung_id: int):
        row = await self.db.fetchone(
            "SELECT id FROM reminders WHERE id=? AND guild_id=? AND user_id=?",
            (erinnerung_id, interaction.guild_id, interaction.user.id),
        )
        if not row:
            raise UserError(f"Keine eigene Erinnerung mit der ID {erinnerung_id} gefunden.")
        await self.db.execute("DELETE FROM reminders WHERE id=?", (erinnerung_id,))
        await interaction.response.send_message(f"Erinnerung #{erinnerung_id} gelöscht.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Reminders(bot))
