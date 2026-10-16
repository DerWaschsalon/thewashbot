"""Giveaways: Embed mit „🎉 Teilnehmen“-Button (Klick an/abmelden), automatische Auslosung nach
Ablauf, `/giveaway-reroll` für neue Gewinner. Der Button-`custom_id` trägt die Nachrichten-ID
selbst, deshalb reicht `register_persistent_views` (einmal pro aktivem Giveaway beim Start), um
nach einem Neustart weiter klickbar zu sein — ganz ohne eigenen Zustand im View.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .reminders import TZ, parse_when
from ..errors import UserError

log = logging.getLogger(__name__)


def build_giveaway_embed(
    prize: str, winner_count: int, ends_at: float, host_id: int | None, *,
    ended: bool = False, winner_ids: list[int] | None = None,
) -> discord.Embed:
    if ended:
        e = discord.Embed(title="🎉 Giveaway beendet", description=f"**{prize}**", colour=discord.Colour.dark_grey())
        e.add_field(name="Gewinner", value=", ".join(f"<@{w}>" for w in winner_ids) if winner_ids else "Niemand hat teilgenommen")
    else:
        e = discord.Embed(title="🎉 Giveaway", description=f"**{prize}**", colour=discord.Colour.gold())
        ends_dt = datetime.fromtimestamp(ends_at, tz=timezone.utc)
        e.add_field(name="Endet", value=discord.utils.format_dt(ends_dt, "R"))
        e.add_field(name="Gewinner-Anzahl", value=str(winner_count))
        if host_id:
            e.add_field(name="Gestartet von", value=f"<@{host_id}>")
    return e


class GiveawayJoinButton(discord.ui.Button):
    def __init__(self, giveaway_id: int):
        super().__init__(label="🎉 Teilnehmen", style=discord.ButtonStyle.success,
                         custom_id=f"giveaway:join:{giveaway_id}")
        self.giveaway_id = giveaway_id

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Giveaways")
        await cog.toggle_entry(interaction, self.giveaway_id)


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: int):
        super().__init__(timeout=None)
        self.add_item(GiveawayJoinButton(giveaway_id))


class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.check_due.start()

    def cog_unload(self):
        self.check_due.cancel()

    async def register_persistent_views(self) -> None:
        rows = await self.db.fetchall("SELECT message_id FROM giveaways WHERE ended=0")
        for row in rows:
            self.bot.add_view(GiveawayView(row["message_id"]), message_id=row["message_id"])

    @app_commands.command(name="giveaway-erstellen", description="Giveaway mit Teilnahme-Button starten")
    @app_commands.describe(
        kanal="Kanal für das Giveaway", preis="Was es zu gewinnen gibt", dauer="z. B. 2h, 30m, 1d2h",
        gewinner="Wie viele Gewinner ausgelost werden (Standard 1)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def create(
        self, interaction: discord.Interaction, kanal: discord.TextChannel, preis: str, dauer: str,
        gewinner: app_commands.Range[int, 1, 20] = 1,
    ):
        if not kanal.permissions_for(interaction.guild.me).send_messages:
            raise UserError(f"Ich darf in {kanal.mention} nicht schreiben.")
        ends_at = parse_when(dauer, datetime.now(TZ)).timestamp()
        message = await kanal.send(embed=build_giveaway_embed(preis, gewinner, ends_at, interaction.user.id))
        view = GiveawayView(message.id)
        await message.edit(view=view)
        self.bot.add_view(view, message_id=message.id)
        await self.db.execute(
            "INSERT INTO giveaways (message_id, guild_id, channel_id, host_id, prize, winners, ends, ended) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (message.id, interaction.guild_id, kanal.id, interaction.user.id, preis, gewinner, ends_at),
        )
        await interaction.response.send_message(f"Giveaway gestartet in {kanal.mention}.", ephemeral=True)

    async def toggle_entry(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        row = await self.db.fetchone("SELECT ended FROM giveaways WHERE message_id=?", (giveaway_id,))
        if not row or row["ended"]:
            return await interaction.response.send_message("Dieses Giveaway ist schon vorbei.", ephemeral=True)
        existing = await self.db.fetchone(
            "SELECT 1 FROM giveaway_entries WHERE message_id=? AND user_id=?", (giveaway_id, interaction.user.id)
        )
        if existing:
            await self.db.execute(
                "DELETE FROM giveaway_entries WHERE message_id=? AND user_id=?", (giveaway_id, interaction.user.id)
            )
            return await interaction.response.send_message("Du nimmst nicht mehr teil.", ephemeral=True)
        await self.db.execute(
            "INSERT INTO giveaway_entries (message_id, user_id) VALUES (?, ?)", (giveaway_id, interaction.user.id)
        )
        await interaction.response.send_message("Du nimmst jetzt teil! 🎉", ephemeral=True)

    async def pick_winners(self, giveaway_id: int, count: int) -> list[int]:
        rows = await self.db.fetchall("SELECT user_id FROM giveaway_entries WHERE message_id=?", (giveaway_id,))
        ids = [r["user_id"] for r in rows]
        random.shuffle(ids)
        return ids[:count]

    @tasks.loop(minutes=1)
    async def check_due(self):
        now = time.time()
        rows = await self.db.fetchall("SELECT * FROM giveaways WHERE ended=0 AND ends<=?", (now,))
        for row in rows:
            await self._finish(row)

    @check_due.before_loop
    async def before_check_due(self):
        await self.bot.wait_until_ready()
        await self.register_persistent_views()

    async def _finish(self, row) -> None:
        await self.db.execute("UPDATE giveaways SET ended=1 WHERE message_id=?", (row["message_id"],))
        winners = await self.pick_winners(row["message_id"], row["winners"])
        channel = self.bot.get_channel(row["channel_id"])
        if not channel:
            return
        try:
            message = await channel.fetch_message(row["message_id"])
            await message.edit(
                embed=build_giveaway_embed(row["prize"], row["winners"], row["ends"], None, ended=True, winner_ids=winners),
                view=None,
            )
        except discord.HTTPException as ex:
            log.warning("Giveaway-Nachricht #%s bearbeiten: %r", row["message_id"], ex)
        text = (f"🎉 Gewinner von **{row['prize']}**: {', '.join(f'<@{w}>' for w in winners)}" if winners
               else f"😢 Niemand hat bei **{row['prize']}** mitgemacht.")
        try:
            await channel.send(text)
        except discord.HTTPException as ex:
            log.warning("Giveaway-Ergebnis #%s posten: %r", row["message_id"], ex)

    @app_commands.command(name="giveaway-reroll", description="Neue Gewinner für ein beendetes Giveaway auslosen")
    @app_commands.describe(giveaway_nachricht_id="Nachrichten-ID des Giveaway-Posts")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def reroll(self, interaction: discord.Interaction, giveaway_nachricht_id: str):
        if not giveaway_nachricht_id.isdigit():
            raise UserError("Das ist keine gültige Nachrichten-ID.")
        message_id = int(giveaway_nachricht_id)
        row = await self.db.fetchone(
            "SELECT * FROM giveaways WHERE message_id=? AND guild_id=?", (message_id, interaction.guild_id)
        )
        if not row:
            raise UserError("Kein Giveaway mit dieser Nachrichten-ID auf diesem Server gefunden.")
        if not row["ended"]:
            raise UserError("Dieses Giveaway läuft noch.")
        winners = await self.pick_winners(message_id, row["winners"])
        if not winners:
            return await interaction.response.send_message("Es gibt keine Teilnehmer zum Auslosen.", ephemeral=True)
        channel = self.bot.get_channel(row["channel_id"])
        text = f"🔁 Neu ausgelost für **{row['prize']}**: {', '.join(f'<@{w}>' for w in winners)}"
        if channel:
            await channel.send(text)
        await interaction.response.send_message("Neu ausgelost.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Giveaways(bot))
