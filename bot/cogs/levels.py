"""Levelsystem: 15-25 XP pro Nachricht (60 s Abklingzeit pro Mitglied), Formel 5·l²+50·l+100
XP für den nächsten Levelaufstieg, Rangkarte (siehe `rankcard.py`), Rollen-Belohnungen,
Kanäle/Rollen ohne XP. Komplett abgeschaltet (`level_system_enabled`), bis ein Server es per
`/setup level` oder im Klick-Panel einschaltet — sonst würde für jeden Server plötzlich ein neues
Verhalten anfangen, ohne dass das irgendwer angefragt hat.
"""

from __future__ import annotations

import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from .rankcard import render_rank_card
from ..errors import UserError

log = logging.getLogger(__name__)

XP_MIN, XP_MAX = 15, 25
XP_COOLDOWN_SECONDS = 60


def xp_for_level(level: int) -> int:
    """XP, um von `level` auf `level + 1` zu kommen (MEE6-Formel)."""
    return 5 * level ** 2 + 50 * level + 100


def level_from_xp(xp: int) -> tuple[int, int, int]:
    """(aktuelles Level, XP im aktuellen Level, für den nächsten Levelaufstieg nötige XP)."""
    level = 0
    remaining = max(0, xp)
    while remaining >= xp_for_level(level):
        remaining -= xp_for_level(level)
        level += 1
    return level, remaining, xp_for_level(level)


class Levels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    # ---- XP vergeben ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return
        guild_id = message.guild.id
        if not self.db.get_bool(guild_id, "level_system_enabled"):
            return
        if message.channel.id in self.db.get_ids(guild_id, "level_no_xp_channels"):
            return
        no_xp_roles = set(self.db.get_ids(guild_id, "level_no_xp_roles"))
        if no_xp_roles and any(r.id in no_xp_roles for r in message.author.roles):
            return

        row = await self.db.fetchone(
            "SELECT xp, last_xp FROM levels WHERE guild_id=? AND user_id=?", (guild_id, message.author.id)
        )
        now = time.time()
        if row and now - (row["last_xp"] or 0) < XP_COOLDOWN_SECONDS:
            await self.db.execute(
                "UPDATE levels SET messages=messages+1 WHERE guild_id=? AND user_id=?", (guild_id, message.author.id)
            )
            return

        old_xp = row["xp"] if row else 0
        new_xp = old_xp + random.randint(XP_MIN, XP_MAX)
        await self.db.execute(
            "INSERT INTO levels (guild_id, user_id, xp, messages, last_xp) VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=excluded.xp, messages=messages+1, last_xp=excluded.last_xp",
            (guild_id, message.author.id, new_xp, now),
        )

        old_level, _, _ = level_from_xp(old_xp)
        new_level, _, _ = level_from_xp(new_xp)
        if new_level > old_level:
            await self._level_up(message, new_level)

    async def _level_up(self, message: discord.Message, new_level: int) -> None:
        guild, member = message.guild, message.author
        channel_id = self.db.get_int(guild.id, "level_announce_channel")
        channel = guild.get_channel(channel_id) if channel_id else message.channel
        if channel:
            try:
                await channel.send(f"🎉 {member.mention} ist jetzt **Level {new_level}**!")
            except discord.HTTPException as ex:
                log.warning("Level-Up-Nachricht %s: %r", guild.id, ex)

        rewards = await self.db.fetchall(
            "SELECT role_id FROM level_rewards WHERE guild_id=? AND level<=?", (guild.id, new_level)
        )
        for r in rewards:
            role = guild.get_role(r["role_id"])
            if role and role not in member.roles and role < guild.me.top_role:
                try:
                    await member.add_roles(role, reason=f"Levelsystem: Level {new_level} erreicht")
                except discord.HTTPException as ex:
                    log.warning("Rollen-Belohnung %s für %s: %r", role.id, member.id, ex)

    # ---- Anzeige ----

    async def _rank_and_xp(self, guild_id: int, user_id: int) -> tuple[int, int]:
        rows = await self.db.fetchall("SELECT user_id, xp FROM levels WHERE guild_id=? ORDER BY xp DESC", (guild_id,))
        xp = next((r["xp"] for r in rows if r["user_id"] == user_id), 0)
        rank = next((i + 1 for i, r in enumerate(rows) if r["user_id"] == user_id), len(rows) + 1)
        return rank, xp

    @app_commands.command(name="rang", description="Deine Rangkarte anzeigen (oder die eines anderen Mitglieds)")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, nutzer: discord.Member | None = None):
        member = nutzer or interaction.user
        rank, xp = await self._rank_and_xp(interaction.guild_id, member.id)
        level, into_level, needed = level_from_xp(xp)
        await interaction.response.defer()
        try:
            avatar_bytes = await member.display_avatar.with_size(256).read()
            image = render_rank_card(avatar_bytes, member.display_name, level, rank, into_level, needed)
            await interaction.followup.send(file=discord.File(image, filename="rang.png"))
        except Exception:
            log.exception("Rangkarte für %s konnte nicht gebaut werden", member.id)
            await interaction.followup.send(f"{member.mention}: Level {level} ({into_level}/{needed} XP), Rang #{rank}.")

    @app_commands.command(name="rangliste", description="Die 10 Mitglieder mit den meisten XP anzeigen")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.db.fetchall(
            "SELECT user_id, xp FROM levels WHERE guild_id=? ORDER BY xp DESC LIMIT 10", (interaction.guild_id,)
        )
        if not rows:
            return await interaction.response.send_message("Noch keine XP gesammelt.", ephemeral=True)
        lines = []
        for i, r in enumerate(rows, start=1):
            member = interaction.guild.get_member(r["user_id"])
            level, _, _ = level_from_xp(r["xp"])
            name = member.mention if member else f"(ausgetreten: {r['user_id']})"
            lines.append(f"**#{i}** {name} – Level {level} ({r['xp']} XP)")
        e = discord.Embed(title="🏆 Rangliste", description="\n".join(lines), colour=discord.Colour.gold())
        await interaction.response.send_message(embed=e)

    # ---- Einstellungen ----

    @app_commands.command(name="level-xp-geben", description="XP eines Mitglieds von Hand ändern (auch negativ)")
    @app_commands.describe(mitglied="Betroffenes Mitglied", menge="XP-Änderung, z. B. 100 oder -50")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def give_xp(
        self, interaction: discord.Interaction, mitglied: discord.Member,
        menge: app_commands.Range[int, -1_000_000, 1_000_000],
    ):
        row = await self.db.fetchone("SELECT xp FROM levels WHERE guild_id=? AND user_id=?", (interaction.guild_id, mitglied.id))
        new_xp = max(0, (row["xp"] if row else 0) + menge)
        await self.db.execute(
            "INSERT INTO levels (guild_id, user_id, xp) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=excluded.xp",
            (interaction.guild_id, mitglied.id, new_xp),
        )
        level, _, _ = level_from_xp(new_xp)
        await interaction.response.send_message(f"{mitglied.mention}: jetzt {new_xp} XP (Level {level}).", ephemeral=True)

    @app_commands.command(name="level-belohnung", description="Rolle als Belohnung ab einem bestimmten Level setzen oder entfernen")
    @app_commands.describe(
        level="Ab diesem Level wird die Rolle vergeben", rolle="Die Belohnungsrolle",
        entfernen="Diese Belohnung wieder entfernen, statt sie zu setzen",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def set_reward(
        self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 1000], rolle: discord.Role,
        entfernen: bool = False,
    ):
        if entfernen:
            await self.db.execute("DELETE FROM level_rewards WHERE guild_id=? AND level=?", (interaction.guild_id, level))
            return await interaction.response.send_message(f"Belohnung für Level {level} entfernt.", ephemeral=True)
        if rolle.managed or rolle >= interaction.guild.me.top_role:
            raise UserError(f"{rolle.mention} kann ich nicht vergeben. Meine Rolle muss in den "
                            "Servereinstellungen über ihr stehen.")
        await self.db.execute(
            "INSERT INTO level_rewards (guild_id, level, role_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, level) DO UPDATE SET role_id=excluded.role_id",
            (interaction.guild_id, level, rolle.id),
        )
        await interaction.response.send_message(f"Ab Level {level}: {rolle.mention}.", ephemeral=True)

    @app_commands.command(name="level-kanal-ohne-xp", description="Kanal zur Liste ohne XP-Sammeln hinzufügen/entfernen")
    @app_commands.describe(kanal="Betroffener Kanal", entfernen="Aus der Liste entfernen statt hinzuzufügen")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def no_xp_channel(self, interaction: discord.Interaction, kanal: discord.TextChannel, entfernen: bool = False):
        ids = self.db.get_ids(interaction.guild_id, "level_no_xp_channels")
        ids = [i for i in ids if i != kanal.id] if entfernen else list(dict.fromkeys(ids + [kanal.id]))
        await self.db.set(interaction.guild_id, "level_no_xp_channels", ",".join(map(str, ids)) or None)
        await interaction.response.send_message(
            f"{kanal.mention} {'sammelt wieder XP.' if entfernen else 'sammelt keine XP mehr.'}", ephemeral=True
        )

    @app_commands.command(name="level-rolle-ohne-xp", description="Rolle zur Liste ohne XP-Sammeln hinzufügen/entfernen")
    @app_commands.describe(rolle="Betroffene Rolle", entfernen="Aus der Liste entfernen statt hinzuzufügen")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def no_xp_role(self, interaction: discord.Interaction, rolle: discord.Role, entfernen: bool = False):
        ids = self.db.get_ids(interaction.guild_id, "level_no_xp_roles")
        ids = [i for i in ids if i != rolle.id] if entfernen else list(dict.fromkeys(ids + [rolle.id]))
        await self.db.set(interaction.guild_id, "level_no_xp_roles", ",".join(map(str, ids)) or None)
        await interaction.response.send_message(
            f"{rolle.mention} {'sammelt wieder XP.' if entfernen else 'sammelt keine XP mehr.'}", ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Levels(bot))
