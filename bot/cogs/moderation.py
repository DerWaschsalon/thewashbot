"""Moderation: Log-Kanal und Spamschutz.

Logs: gelöschte und bearbeitete Nachrichten, Beitritte, Austritte, Banns, Rollenänderungen.
Spamschutz (abschaltbar mit /setup spamschutz) greift nicht bei Bots und bei Leuten mit
„Nachrichten verwalten“:
  - mehr als SPAM_COUNT Nachrichten in SPAM_WINDOW Sekunden → Nachrichten weg, 5 Min. Timeout
  - mehr als MENTION_LIMIT Erwähnungen in einer Nachricht   → Nachricht weg, 10 Min. Timeout
  - Discord-Einladungslinks (außer mit /setup … einladungslinks_erlauben) → Nachricht weg
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

SPAM_COUNT = 6
SPAM_WINDOW = 8.0
MENTION_LIMIT = 6
INVITE_RE = re.compile(r"(?:discord(?:app)?\.com/invite|discord\.gg)/[\w-]+", re.I)


def clip(text: str | None, limit: int = 1000) -> str:
    if not text:
        return "*(kein Text)*"
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self._recent: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))
        self._own_deletes: set[int] = set()  # vom Spamschutz gelöscht, nicht einzeln loggen

    async def log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        channel_id = self.db.get_int(guild.id, "log_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel:
            embed.timestamp = discord.utils.utcnow()
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                log.warning("Log %s: %r", guild.id, e)

    def _is_log_channel(self, channel) -> bool:
        return channel and channel.id == self.db.get_int(channel.guild.id, "log_channel")

    # ---- Spamschutz ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
            return
        guild = message.guild
        if not self.db.get_bool(guild.id, "antispam", True):
            return
        if message.channel.permissions_for(message.author).manage_messages:
            return

        if not self.db.get_bool(guild.id, "allow_invites") and INVITE_RE.search(message.content):
            await self._punish(message, [message], None, "Einladungslink")
            return

        if len(message.raw_mentions) + len(message.raw_role_mentions) > MENTION_LIMIT:
            await self._punish(message, [message], timedelta(minutes=10), "Massen-Erwähnung")
            return

        key = (guild.id, message.author.id)
        recent = self._recent[key]
        now = time.monotonic()
        recent.append((now, message))
        in_window = [m for t, m in recent if now - t <= SPAM_WINDOW]
        if len(in_window) > SPAM_COUNT:
            recent.clear()
            await self._punish(message, in_window, timedelta(minutes=5), "Spam")

    async def _punish(self, message: discord.Message, messages: list[discord.Message],
                      timeout: timedelta | None, reason: str) -> None:
        member = message.author
        me = message.guild.me
        for m in messages:
            self._own_deletes.add(m.id)
            try:
                await m.delete()
            except discord.HTTPException:
                pass
        did_timeout = False
        if timeout and me.guild_permissions.moderate_members and member.top_role < me.top_role:
            try:
                await member.timeout(timeout, reason=f"Spamschutz: {reason}")
                did_timeout = True
            except discord.HTTPException:
                pass
        notice = f"{member.mention}, deine Nachricht wurde entfernt ({reason})."
        if did_timeout:
            notice += f" Du kannst {int(timeout.total_seconds() // 60)} Minuten nicht schreiben."
        try:
            await message.channel.send(notice, delete_after=15)
        except discord.HTTPException:
            pass
        e = discord.Embed(title=f"🛡️ Spamschutz: {reason}", colour=discord.Colour.orange(),
                          description=clip(message.content, 500))
        e.add_field(name="Mitglied", value=f"{member.mention} ({member})")
        e.add_field(name="Kanal", value=message.channel.mention)
        e.add_field(name="Maßnahme", value=f"{len(messages)} Nachricht(en) gelöscht"
                    + (f", Timeout {int(timeout.total_seconds() // 60)} Min." if did_timeout else ""))
        await self.log(message.guild, e)

    # ---- Logs ----

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.id in self._own_deletes or message.id in self.bot.quiet_deletes:
            self._own_deletes.discard(message.id)
            self.bot.quiet_deletes.discard(message.id)
            return
        if not message.guild or message.author.bot or self._is_log_channel(message.channel):
            return
        e = discord.Embed(title="🗑️ Nachricht gelöscht", colour=discord.Colour.red(), description=clip(message.content))
        e.add_field(name="Von", value=f"{message.author.mention} ({message.author})")
        e.add_field(name="Kanal", value=message.channel.mention)
        if message.attachments:
            e.add_field(name="Anhänge", value="\n".join(a.filename for a in message.attachments)[:1000], inline=False)
        await self.log(message.guild, e)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        if not messages or not messages[0].guild or self._is_log_channel(messages[0].channel):
            return
        e = discord.Embed(title="🗑️ Mehrere Nachrichten gelöscht", colour=discord.Colour.red(),
                          description=f"{len(messages)} Nachrichten in {messages[0].channel.mention}")
        await self.log(messages[0].guild, e)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or after.author.bot or before.content == after.content:
            return
        e = discord.Embed(title="✏️ Nachricht bearbeitet", colour=discord.Colour.gold(), url=after.jump_url)
        e.add_field(name="Vorher", value=clip(before.content), inline=False)
        e.add_field(name="Nachher", value=clip(after.content), inline=False)
        e.add_field(name="Von", value=f"{after.author.mention} ({after.author})")
        e.add_field(name="Kanal", value=after.channel.mention)
        await self.log(after.guild, e)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        age = discord.utils.utcnow() - member.created_at
        e = discord.Embed(title="📥 Beigetreten", colour=discord.Colour.green(),
                          description=f"{member.mention} ({member})")
        e.add_field(name="Konto erstellt", value=discord.utils.format_dt(member.created_at, "R")
                    + (" ⚠️ neu" if age < timedelta(days=7) else ""))
        e.set_thumbnail(url=member.display_avatar.url)
        await self.log(member.guild, e)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        roles = [r.mention for r in member.roles if not r.is_default()]
        e = discord.Embed(title="📤 Verlassen", colour=discord.Colour.dark_grey(),
                          description=f"{member.mention} ({member})")
        if member.joined_at:
            e.add_field(name="Dabei seit", value=discord.utils.format_dt(member.joined_at, "R"))
        if roles:
            e.add_field(name="Rollen", value=" ".join(roles)[:1000], inline=False)
        await self.log(member.guild, e)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        await self.log(guild, discord.Embed(title="🔨 Gebannt", colour=discord.Colour.dark_red(),
                                            description=f"{user.mention} ({user})"))

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        await self.log(guild, discord.Embed(title="🔓 Entbannt", colour=discord.Colour.teal(),
                                            description=f"{user.mention} ({user})"))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        changes = []
        if added:
            changes.append("➕ " + " ".join(r.mention for r in added))
        if removed:
            changes.append("➖ " + " ".join(r.mention for r in removed))
        if before.nick != after.nick:
            changes.append(f"Spitzname: {before.nick or '–'} → {after.nick or '–'}")
        if not changes:
            return
        e = discord.Embed(title="👤 Mitglied geändert", colour=discord.Colour.blue(),
                          description=f"{after.mention} ({after})\n" + "\n".join(changes))
        await self.log(after.guild, e)

    # ---- Befehle ----

    @app_commands.command(name="timeout", description="Mitglied für eine Weile stummschalten")
    @app_commands.describe(minuten="Dauer (1–40320 = 4 Wochen)", grund="Wird im Log vermerkt")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def timeout_cmd(self, interaction: discord.Interaction, mitglied: discord.Member,
                          minuten: app_commands.Range[int, 1, 40320], grund: str | None = None):
        if mitglied.top_role >= interaction.guild.me.top_role or mitglied == interaction.guild.owner:
            return await interaction.response.send_message("Dieses Mitglied kann ich nicht stummschalten.", ephemeral=True)
        await mitglied.timeout(timedelta(minutes=minuten), reason=f"{interaction.user}: {grund or 'ohne Grund'}")
        await interaction.response.send_message(f"{mitglied.mention} ist für {minuten} Min. stumm.", ephemeral=True)
        e = discord.Embed(title="⏳ Timeout", colour=discord.Colour.orange(),
                          description=f"{mitglied.mention} für {minuten} Min.\nVon {interaction.user.mention}"
                                      + (f"\nGrund: {grund}" if grund else ""))
        await self.log(interaction.guild, e)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
