"""Gruppen-DPS von DeckBar (github.com/gamunex/deckbar) nach Discord posten.

DeckBar schickt bei Kampfende den Endstand der eigenen Gruppe (ein Eintrag pro Mitglied, nicht
pro einzelnem Spieler in der Nähe) per POST an http://<server>:<DPS_WEBHOOK_PORT>/dps/<token>.
Der Token in der URL identifiziert Server+Kanal (siehe /setup dps-webhook), daher braucht der
Payload selbst keine Discord-IDs.

Erwarteter Payload:
    {"target": "Bossname", "duration_s": 83,
     "party": [{"name": "…", "cls": "cleric", "dps": 18432, "dmg": 1529856, "pct": 31.2}]}
"""

from __future__ import annotations

import logging
import os
import secrets

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)
DEFAULT_PORT = 8787

CLASS_EMOJI = {
    "gladiator": "⚔️", "templar": "🛡️", "ranger": "🏹", "assassin": "🗡️", "sorcerer": "🔥",
    "cleric": "✨", "spiritmaster": "🌙", "chanter": "🎵", "brawler": "👊",
}


def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def build_embed(data: dict) -> discord.Embed | None:
    party = data.get("party")
    if not isinstance(party, list) or not party:
        return None
    rows = sorted(party, key=lambda r: -float(r.get("dps") or 0))
    lines = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        emoji = CLASS_EMOJI.get(r.get("cls"), "•")
        name = str(r.get("name") or "?")[:32]
        dps, dmg = int(r.get("dps") or 0), int(r.get("dmg") or 0)
        pct = float(r.get("pct") or 0)
        lines.append(f"{emoji} **{name}** — {fmt_int(dps)} DPS ({fmt_int(dmg)} Schaden, {pct:.1f}%)")
    if not lines:
        return None
    target = str(data.get("target") or "Kampf")[:100]
    e = discord.Embed(title=f"⚔️ {target}", colour=discord.Colour.red(), description="\n".join(lines)[:4000])
    duration = data.get("duration_s")
    if isinstance(duration, (int, float)) and duration > 0:
        e.set_footer(text=f"Kampfdauer: {int(duration)} s · DeckBar")
    else:
        e.set_footer(text="DeckBar")
    return e


class DpsWebhook(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.runner: web.AppRunner | None = None

    async def cog_load(self) -> None:
        app = web.Application()
        app.router.add_post("/dps/{token}", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        port = int(os.getenv("DPS_WEBHOOK_PORT", DEFAULT_PORT))
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()
        log.info("DPS-Webhook hört auf Port %d", port)

    async def cog_unload(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def _handle(self, request: web.Request) -> web.Response:
        guild_id = self.db.guild_for_value("dps_webhook_token", request.match_info["token"])
        if guild_id is None:
            return web.json_response({"error": "unknown token"}, status=404)
        channel_id = self.db.get_int(guild_id, "dps_webhook_channel")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not channel:
            return web.json_response({"error": "channel not configured"}, status=410)
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "invalid json"}, status=400)
        embed = build_embed(data)
        if embed is None:
            return web.json_response({"error": "party required"}, status=400)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.warning("DPS-Post für Server %s: %r", guild_id, e)
            return web.json_response({"error": "send failed"}, status=502)
        return web.json_response({"ok": True})


@app_commands.command(name="dps-webhook", description="Kanal für DeckBar-Gruppen-DPS festlegen")
@app_commands.describe(kanal="Kanal, in den die Gruppen-DPS nach jedem Kampf gepostet wird")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def set_dps_webhook(interaction: discord.Interaction, kanal: discord.TextChannel):
    db = interaction.client.db
    token = db.get(interaction.guild_id, "dps_webhook_token")
    if not token:
        token = secrets.token_urlsafe(24)
        await db.set(interaction.guild_id, "dps_webhook_token", token)
    await db.set(interaction.guild_id, "dps_webhook_channel", kanal.id)
    host = os.getenv("DPS_WEBHOOK_HOST") or "<LAN-IP-DIESES-SERVERS>"
    port = os.getenv("DPS_WEBHOOK_PORT", str(DEFAULT_PORT))
    url = f"http://{host}:{port}/dps/{token}"
    await interaction.response.send_message(
        f"Gruppen-DPS geht jetzt nach {kanal.mention}.\n\nURL für die DeckBar-Einstellungen:\n`{url}`\n\n"
        "Geheim halten — wer die URL hat, kann hier posten. `DPS_WEBHOOK_HOST` in der `.env` setzen, "
        "damit hier die echte IP statt des Platzhalters steht.", ephemeral=True,
    )


async def setup(bot):
    await bot.add_cog(DpsWebhook(bot))
    bot.tree.add_command(set_dps_webhook)
