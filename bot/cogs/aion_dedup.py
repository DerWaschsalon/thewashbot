"""AION-2-Duplikatfilter: mehrere DeckBar-Posts vom selben Dungeon-Lauf zu einem zusammenfassen.

Kein eigenes Gruppensystem, keine eigene API, keine WebUI — DeckBar postet weiterhin unverändert
über seinen eigenen Discord-Webhook (siehe github.com/gamunex/deckbar, `daemon/deckbar_daemon/runs.py`,
Funktion `run_message`/`post_discord`). Dieser Cog beobachtet nur den dafür festgelegten Kanal: taucht
dort eine Webhook-Nachricht von "DeckBar" auf, wird sie geparst und kurz zurückgehalten. Erkennt der
Bot innerhalb der Wartezeit eine weitere Nachricht vom selben Lauf (gleicher Dungeon, gleiche
Boss-Anzahl, ähnliche Dauer/Startzeit), wird nur die zuerst angekommene behalten, der Rest gelöscht.

Format der DeckBar-Nachricht (Stand der analysierten Version), Beispiel:
    **Ferocious Horn Den** – 3/3 Bosse besiegt, Kampfzeit 5:50 (15:42–15:48)
Die Startzeit darin hat nur Minuten-Genauigkeit (keine Sekunden) — das Matching toleriert das.

Hinweis: Der von Dave ursprünglich gewünschte Spieler-Filter (bestimmte Namen bevorzugt matchen)
lässt sich damit nicht umsetzen — die Spielernamen stehen nur im angehängten Bild, nicht im Text,
und das Bild wird hier nicht ausgelesen.
"""

from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

RUN_RE = re.compile(
    r"^\*\*(?P<dungeon>.+?)\*\*\s+–\s+(?P<kills>\d+)/(?P<total>\d+)\s+(?:Boss|Bosse)\s+besiegt,\s+"
    r"Kampfzeit\s+(?P<dmin>\d+):(?P<dsec>\d{2})\s+\((?P<start>\d{1,2}:\d{2})–(?P<end>\d{1,2}:\d{2})\)"
)
DEFAULT_DELAY = 15
DURATION_TOLERANCE_S = 20
START_TOLERANCE_MIN = 1


def parse_run(content: str) -> dict | None:
    m = RUN_RE.match(content.strip())
    if not m:
        return None
    hh, mm = m["start"].split(":")
    start_min = int(hh) * 60 + int(mm)
    return {
        "dungeon": m["dungeon"], "kills": int(m["kills"]), "total": int(m["total"]),
        "duration_s": int(m["dmin"]) * 60 + int(m["dsec"]), "start_min": start_min,
    }


def same_run(a: dict, b: dict) -> bool:
    if a["dungeon"] != b["dungeon"] or a["kills"] != b["kills"] or a["total"] != b["total"]:
        return False
    if abs(a["duration_s"] - b["duration_s"]) > DURATION_TOLERANCE_S:
        return False
    # Minuten-Rollover (z. B. 23:59 vs. 00:01) über den Tagesrand ignorieren wir bewusst — ein
    # Dungeon-Lauf, der genau um Mitternacht beginnt, ist selten genug, das in Kauf zu nehmen.
    if abs(a["start_min"] - b["start_min"]) > START_TOLERANCE_MIN:
        return False
    return True


class Cluster:
    def __init__(self, run: dict, keeper: discord.Message):
        self.run = run
        self.keeper = keeper
        self.duplicates: list[discord.Message] = []
        self.task: asyncio.Task | None = None


class AionDedup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.pending: dict[int, list[Cluster]] = {}  # guild_id -> offene Cluster

    def cog_unload(self):
        for clusters in self.pending.values():
            for c in clusters:
                if c.task:
                    c.task.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.webhook_id is None or not message.guild:
            return
        channel_id = self.db.get_int(message.guild.id, "aion_channel")
        if not channel_id or message.channel.id != channel_id:
            return
        if (message.author.name or "").strip().lower() != "deckbar":
            return
        run = parse_run(message.content or "")
        if run is None:
            return

        clusters = self.pending.setdefault(message.guild.id, [])
        for c in clusters:
            if same_run(c.run, run):
                c.duplicates.append(message)
                log.info("AION: %s als Duplikat von %s erkannt (%s)", message.id, c.keeper.id, run["dungeon"])
                return

        delay = self.db.get_int(message.guild.id, "aion_dedup_delay") or DEFAULT_DELAY
        cluster = Cluster(run, message)
        clusters.append(cluster)
        cluster.task = asyncio.create_task(self._finalize_later(message.guild.id, cluster, delay))

    async def _finalize_later(self, guild_id: int, cluster: Cluster, delay: int) -> None:
        await asyncio.sleep(delay)
        clusters = self.pending.get(guild_id, [])
        if cluster in clusters:
            clusters.remove(cluster)
        if not cluster.duplicates:
            return
        log.info(
            "AION: %s – behalte %s, lösche %d Duplikat(e)",
            cluster.run["dungeon"], cluster.keeper.id, len(cluster.duplicates),
        )
        for dup in cluster.duplicates:
            try:
                await dup.delete()
            except discord.HTTPException as e:
                log.warning("AION: Duplikat %s konnte nicht gelöscht werden: %r", dup.id, e)

    @app_commands.command(name="aion", description="AION-2-Duplikatfilter für DeckBar-Posts einrichten")
    @app_commands.describe(
        kanal="Kanal, in dem DeckBar seine Dungeon-Run-Posts per Webhook abliefert",
        wartezeit="Sekunden, die auf weitere Posts vom selben Lauf gewartet wird (Standard 15)",
        aus="Duplikatfilter abschalten",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def aion_settings(
        self, interaction: discord.Interaction, kanal: discord.TextChannel | None = None,
        wartezeit: app_commands.Range[int, 5, 300] | None = None, aus: bool = False,
    ):
        if aus:
            await self.db.set(interaction.guild_id, "aion_channel", None)
            return await interaction.response.send_message("AION-Duplikatfilter ist aus.", ephemeral=True)
        if kanal:
            await self.db.set(interaction.guild_id, "aion_channel", kanal.id)
        if wartezeit:
            await self.db.set(interaction.guild_id, "aion_dedup_delay", wartezeit)
        channel_id = self.db.get_int(interaction.guild_id, "aion_channel")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        delay = self.db.get_int(interaction.guild_id, "aion_dedup_delay") or DEFAULT_DELAY
        if not channel:
            return await interaction.response.send_message(
                "Noch kein Kanal festgelegt. `/aion kanal:#dein-deckbar-kanal`.", ephemeral=True
            )
        await interaction.response.send_message(
            f"AION-Duplikatfilter aktiv in {channel.mention}, Wartezeit {delay}s.\n"
            "DeckBar muss weiterhin ganz normal per eigenem Discord-Webhook dort posten "
            "(„Dungeon-Runs → Discord“ in DeckBars Einstellungen) — nichts an DeckBar ändern.",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(AionDedup(bot))
