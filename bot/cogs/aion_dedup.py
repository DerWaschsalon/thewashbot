"""AION-2: mehrere DeckBar-Posts vom selben Dungeon-Lauf zu einem zusammenfassen.

Zwei unabhängige Wege, je nachdem ob DeckBar selbst geändert wurde:

1. **Nachrichten-Filter** (kein DeckBar-Eingriff nötig): DeckBar postet weiterhin unverändert
   über seinen eigenen Discord-Webhook (github.com/gamunex/deckbar,
   `daemon/deckbar_daemon/runs.py`, `run_message`/`post_discord`). Dieser Cog beobachtet den per
   `/aion kanal:` festgelegten Kanal, erkennt mehrere Nachrichten vom selben Lauf (Dungeon,
   Boss-Anzahl, ähnliche Dauer/Startzeit aus dem Text) und löscht alle außer der zuerst
   angekommenen. Format-Beispiel:
       **Ferocious Horn Den** – 3/3 Bosse besiegt, Kampfzeit 5:50 (15:42–15:48)
   Spielernamen stehen hier nur im angehängten Bild, nicht im Text — kein Namens-Filter möglich.

2. **API** (braucht eine Änderung an DeckBar, die an gamunex geht): `POST /api/aion/fight` auf
   diesem Server (Port 8789, siehe `bot/cogs/dashboard.py`) nimmt strukturierte Lauf-Daten mit
   echten Spielernamen/Schaden entgegen, merged mehrere Meldungen desselben Laufs zu einer
   Rangliste und postet genau einmal ein eigenes Embed. Schutz über einen gemeinsamen Token
   (`/aion token`), da der Endpunkt über Cloudflare öffentlich erreichbar ist.

   Erwarteter Payload (ein POST pro abgeschlossenem Lauf, eine Zeile pro Spieler):
       {
         "dungeon": "Ferocious Horn Den", "difficulty": "Normal",
         "bosses_total": 3, "bosses_killed": 3,
         "start_ms": 1760445123000, "fight_ms": 350000,
         "players": [{"name": "ReZero", "damage": 13290000, "dps": 38000}, …],
         "bosses": [                                          # optional, für DPS pro Boss
           {"name": "Boss 1", "players": [{"name": "ReZero", "damage": 4200000, "dps": 41000}, …]},
           …
         ]
       }
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time

import discord
from aiohttp import web
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
API_START_TOLERANCE_MS = 30_000
MEDALS = ["🥇", "🥈", "🥉"]


def fmt_num(n: float) -> str:
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:.0f}"


def fmt_dps(n: float) -> str:
    return f"{fmt_num(n)}/s"


# ---- Variante 1: Text-Nachrichten im bestehenden DeckBar-Webhook-Kanal filtern --------------


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


# ---- Variante 2: strukturierte API --------------------------------------------------------


def validate_fight_payload(data: dict) -> str | None:
    """Gibt eine Fehlermeldung zurück, oder None wenn der Payload brauchbar ist."""
    if not isinstance(data.get("dungeon"), str) or not data["dungeon"].strip():
        return "dungeon fehlt"
    for key in ("bosses_total", "bosses_killed", "start_ms", "fight_ms"):
        if not isinstance(data.get(key), (int, float)):
            return f"{key} fehlt oder ist keine Zahl"
    players = data.get("players")
    if not isinstance(players, list) or not players:
        return "players fehlt oder ist leer"
    for p in players:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
            return "players[].name fehlt"
        if not isinstance(p.get("damage"), (int, float)):
            return "players[].damage fehlt oder ist keine Zahl"
    bosses = data.get("bosses")
    if bosses is not None:
        if not isinstance(bosses, list):
            return "bosses muss eine Liste sein"
        for b in bosses:
            if not isinstance(b, dict) or not isinstance(b.get("name"), str) or not b["name"].strip():
                return "bosses[].name fehlt"
            boss_players = b.get("players")
            if not isinstance(boss_players, list) or not boss_players:
                return "bosses[].players fehlt oder ist leer"
            for p in boss_players:
                if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
                    return "bosses[].players[].name fehlt"
                if not isinstance(p.get("damage"), (int, float)):
                    return "bosses[].players[].damage fehlt oder ist keine Zahl"
    return None


def same_api_run(a: dict, b: dict) -> bool:
    if a["dungeon"] != b["dungeon"] or a["bosses_total"] != b["bosses_total"] or a["bosses_killed"] != b["bosses_killed"]:
        return False
    if abs(a["fight_ms"] - b["fight_ms"]) > DURATION_TOLERANCE_S * 1000:
        return False
    if abs(a["start_ms"] - b["start_ms"]) > API_START_TOLERANCE_MS:
        return False
    return True


def merge_players(reports: list[dict]) -> list[dict]:
    """Ein Eintrag pro Spielername, über alle Meldungen desselben Laufs — bei Abweichungen
    (unterschiedliche lokale Mitschnitte) gewinnt der höhere Schadenswert."""
    best: dict[str, dict] = {}
    for report in reports:
        for p in report["players"]:
            name = p["name"].strip()
            cur = best.get(name)
            if cur is None or p["damage"] > cur["damage"]:
                best[name] = {"name": name, "damage": p["damage"], "dps": p.get("dps") or 0}
    return sorted(best.values(), key=lambda p: -p["damage"])


def merge_bosses(reports: list[dict]) -> list[dict]:
    """Pro Boss (Reihenfolge nach erster Meldung) die Spieler mergen wie bei merge_players."""
    order: list[str] = []
    per_boss: dict[str, list[dict]] = {}
    for report in reports:
        for b in report.get("bosses") or []:
            name = b["name"].strip()
            if name not in per_boss:
                per_boss[name] = []
                order.append(name)
            per_boss[name].append({"players": b["players"]})
    return [{"name": name, "players": merge_players(per_boss[name])} for name in order]


def build_run_embed(
    run: dict, players: list[dict], reporters: int, bosses: list[dict] | None = None
) -> discord.Embed:
    title = f"⚔️ {run['dungeon']}" + (f" ({run['difficulty']})" if run.get("difficulty") else "")
    lines = []
    for i, p in enumerate(players[:15]):
        rank = MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
        lines.append(f"{rank} **{p['name']}**  {fmt_num(p['damage'])}  {fmt_dps(p['dps'])}")
    minutes, seconds = divmod(round(run["fight_ms"] / 1000), 60)
    e = discord.Embed(title=title, colour=discord.Colour.gold(), description="\n".join(lines))
    e.add_field(name="Bosse", value=f"{run['bosses_killed']}/{run['bosses_total']} besiegt")
    e.add_field(name="Kampfzeit", value=f"{minutes}:{seconds:02d}")
    for b in (bosses or [])[:20]:
        boss_lines = [f"**{p['name']}**  {fmt_dps(p['dps'])}" for p in b["players"][:10]]
        e.add_field(name=f"🗡️ {b['name']}", value="\n".join(boss_lines) or "–", inline=False)
    e.set_footer(text=f"AION-2 · {reporters} DeckBar-Meldung(en) zusammengeführt")
    return e


class ApiCluster:
    def __init__(self, run: dict, guild_id: int):
        self.run = run
        self.guild_id = guild_id
        self.reports: list[dict] = [run]
        self.task: asyncio.Task | None = None


class AionDedup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.pending: dict[int, list[Cluster]] = {}  # guild_id -> offene Text-Cluster
        self.api_pending: dict[int, list[ApiCluster]] = {}  # guild_id -> offene API-Cluster

    def cog_unload(self):
        for clusters in self.pending.values():
            for c in clusters:
                if c.task:
                    c.task.cancel()
        for clusters in self.api_pending.values():
            for c in clusters:
                if c.task:
                    c.task.cancel()

    # ---- Variante 1: Nachrichten im DeckBar-Kanal -----------------------------------------

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

    # ---- Variante 2: API -------------------------------------------------------------------

    def api_url(self) -> str:
        # öffentliche Adresse (z. B. über Cloudflare Tunnel), NICHT die LAN-IP — die Gegenseite
        # (DeckBar bei anderen Spielern) sitzt oft außerhalb des eigenen Netzes.
        base = os.getenv("AION_PUBLIC_URL", "").rstrip("/") or "<AION_PUBLIC_URL fehlt in .env>"
        return f"{base}/api/aion/fight"

    async def handle_api_fight(self, request: web.Request) -> web.Response:
        guild_id = self.db.guild_for_value("aion_api_token", (request.headers.get("Authorization") or "")[7:])
        if guild_id is None or not (request.headers.get("Authorization") or "").startswith("Bearer "):
            return web.json_response({"error": "unauthorized"}, status=401)
        channel_id = self.db.get_int(guild_id, "aion_channel")
        if not channel_id:
            return web.json_response({"error": "kein aion_channel eingerichtet (/aion kanal:)"}, status=409)
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "invalid json"}, status=400)
        error = validate_fight_payload(data)
        if error:
            return web.json_response({"error": error}, status=400)

        run = {
            "dungeon": data["dungeon"].strip(), "difficulty": (data.get("difficulty") or "").strip() or None,
            "bosses_total": int(data["bosses_total"]), "bosses_killed": int(data["bosses_killed"]),
            "start_ms": int(data["start_ms"]), "fight_ms": int(data["fight_ms"]), "players": data["players"],
            "bosses": data.get("bosses") or [],
        }
        clusters = self.api_pending.setdefault(guild_id, [])
        for c in clusters:
            if same_api_run(c.run, run):
                c.reports.append(run)
                log.info("AION-API: Lauf %s bekommt weitere Meldung (jetzt %d)", run["dungeon"], len(c.reports))
                return web.json_response({"ok": True, "merged": True})

        delay = self.db.get_int(guild_id, "aion_dedup_delay") or DEFAULT_DELAY
        cluster = ApiCluster(run, guild_id)
        clusters.append(cluster)
        cluster.task = asyncio.create_task(self._finalize_api_later(cluster, delay, channel_id))
        return web.json_response({"ok": True, "merged": False})

    async def _finalize_api_later(self, cluster: ApiCluster, delay: int, channel_id: int) -> None:
        await asyncio.sleep(delay)
        clusters = self.api_pending.get(cluster.guild_id, [])
        if cluster in clusters:
            clusters.remove(cluster)
        channel = self.bot.get_channel(channel_id)
        if not channel:
            log.warning("AION-API: Zielkanal %s nicht gefunden", channel_id)
            return
        players = merge_players(cluster.reports)
        bosses = merge_bosses(cluster.reports)
        embed = build_run_embed(cluster.run, players, len(cluster.reports), bosses)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.warning("AION-API: Posten fehlgeschlagen: %r", e)

    # ---- Einrichtung -----------------------------------------------------------------------

    @app_commands.command(name="aion", description="AION-2-Duplikatfilter für DeckBar-Posts einrichten")
    @app_commands.describe(
        kanal="Kanal für den Duplikatfilter bzw. die zusammengeführten AION-Posts",
        wartezeit="Sekunden, die auf weitere Posts vom selben Lauf gewartet wird (Standard 15)",
        token="Neuen API-Token erzeugen (alten ungültig machen), für die DeckBar-API-Anbindung",
        aus="Duplikatfilter abschalten",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def aion_settings(
        self, interaction: discord.Interaction, kanal: discord.TextChannel | None = None,
        wartezeit: app_commands.Range[int, 5, 300] | None = None, token: bool = False, aus: bool = False,
    ):
        if aus:
            await self.db.set(interaction.guild_id, "aion_channel", None)
            return await interaction.response.send_message("AION-Duplikatfilter ist aus.", ephemeral=True)
        if kanal:
            await self.db.set(interaction.guild_id, "aion_channel", kanal.id)
        if wartezeit:
            await self.db.set(interaction.guild_id, "aion_dedup_delay", wartezeit)

        lines = []
        channel_id = self.db.get_int(interaction.guild_id, "aion_channel")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        delay = self.db.get_int(interaction.guild_id, "aion_dedup_delay") or DEFAULT_DELAY
        if not channel:
            return await interaction.response.send_message(
                "Noch kein Kanal festgelegt. `/aion kanal:#dein-deckbar-kanal`.", ephemeral=True
            )
        lines.append(f"Kanal: {channel.mention}, Wartezeit {delay}s.")
        lines.append(
            "Variante 1 (DeckBar unverändert): postet weiter selbst per eigenem Webhook in diesen Kanal, "
            "der Bot löscht Duplikate."
        )

        api_token = self.db.get(interaction.guild_id, "aion_api_token")
        if token or not api_token:
            api_token = secrets.token_urlsafe(24)
            await self.db.set(interaction.guild_id, "aion_api_token", api_token)
        lines.append(
            f"\nVariante 2 (API, braucht eine DeckBar-Änderung):\n`POST {self.api_url()}`\n"
            f"`Authorization: Bearer {api_token}`\n"
            "Geheim halten, nur an DeckBar-Instanzen geben, die mitmachen sollen. Mit `token:True` "
            "einen neuen erzeugen (macht den alten ungültig)."
        )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot):
    await bot.add_cog(AionDedup(bot))
