"""Zeitgesteuerte Ankündigungen: ein Text wird alle X Minuten automatisch in einen Kanal gepostet.
Nutzt die schon vorhandene `scheduled`-Tabelle (`next_run` als Unix-Zeitstempel).
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..errors import UserError

log = logging.getLogger(__name__)


class Announcements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.check_due.start()

    def cog_unload(self):
        self.check_due.cancel()

    @app_commands.command(name="ankuendigung-erstellen", description="Text alle X Minuten automatisch in einen Kanal posten")
    @app_commands.describe(
        kanal="Kanal für die Ankündigung",
        text="Der anzukündigende Text",
        intervall_minuten="Alle wie viele Minuten gepostet wird (mindestens 5)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def create(
        self, interaction: discord.Interaction, kanal: discord.TextChannel, text: str,
        intervall_minuten: app_commands.Range[int, 5, 10080],
    ):
        if not kanal.permissions_for(interaction.guild.me).send_messages:
            raise UserError(f"Ich darf in {kanal.mention} nicht schreiben.")
        row_id = await self.db.execute(
            "INSERT INTO scheduled (guild_id, channel_id, text, interval_minutes, next_run) VALUES (?, ?, ?, ?, ?)",
            (interaction.guild_id, kanal.id, text, intervall_minuten, time.time()),
        )
        await interaction.response.send_message(
            f"Ankündigung #{row_id} angelegt: alle {intervall_minuten} Min. in {kanal.mention}.", ephemeral=True
        )

    @app_commands.command(name="ankuendigungen", description="Alle zeitgesteuerten Ankündigungen dieses Servers anzeigen")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def list_announcements(self, interaction: discord.Interaction):
        rows = await self.db.fetchall(
            "SELECT id, channel_id, interval_minutes, next_run, text FROM scheduled WHERE guild_id=? ORDER BY id",
            (interaction.guild_id,),
        )
        if not rows:
            return await interaction.response.send_message("Keine Ankündigungen eingerichtet.", ephemeral=True)
        lines = []
        for r in rows:
            channel = interaction.guild.get_channel(r["channel_id"])
            lines.append(
                f"**#{r['id']}** {channel.mention if channel else '(gelöschter Kanal)'} – "
                f"alle {r['interval_minutes']} Min. – „{r['text'][:60]}“"
            )
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @app_commands.command(name="ankuendigung-loeschen", description="Zeitgesteuerte Ankündigung per ID löschen")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def delete_announcement(self, interaction: discord.Interaction, ankuendigung_id: int):
        row = await self.db.fetchone(
            "SELECT id FROM scheduled WHERE id=? AND guild_id=?", (ankuendigung_id, interaction.guild_id)
        )
        if not row:
            raise UserError(f"Keine Ankündigung mit der ID {ankuendigung_id} auf diesem Server gefunden.")
        await self.db.execute("DELETE FROM scheduled WHERE id=?", (ankuendigung_id,))
        await interaction.response.send_message(f"Ankündigung #{ankuendigung_id} gelöscht.", ephemeral=True)

    @tasks.loop(minutes=1)
    async def check_due(self):
        now = time.time()
        rows = await self.db.fetchall("SELECT * FROM scheduled WHERE next_run <= ?", (now,))
        for row in rows:
            await self._post(row)
            await self.db.execute(
                "UPDATE scheduled SET next_run=? WHERE id=?", (now + row["interval_minutes"] * 60, row["id"])
            )

    @check_due.before_loop
    async def before_check_due(self):
        await self.bot.wait_until_ready()

    async def _post(self, row) -> None:
        channel = self.bot.get_channel(row["channel_id"])
        if not channel:
            return
        try:
            await channel.send(row["text"])
        except discord.HTTPException as ex:
            log.warning("Ankündigung #%s: %r", row["id"], ex)


async def setup(bot):
    await bot.add_cog(Announcements(bot))
