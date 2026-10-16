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
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from ..errors import UserError

log = logging.getLogger(__name__)

SPAM_COUNT = 6
SPAM_WINDOW = 8.0
MENTION_LIMIT = 6
INVITE_RE = re.compile(r"(?:discord(?:app)?\.com/invite|discord\.gg)/[\w-]+", re.I)
LINK_RE = re.compile(r"https?://([\w.-]+)", re.I)
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF]|<a?:\w+:\d+>")
CAPS_MIN_LETTERS = 10
CAPS_RATIO = 0.7


def caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < CAPS_MIN_LETTERS:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def domain_allowed(domain: str, allowed: set[str]) -> bool:
    domain = domain.lower()
    return any(domain == a or domain.endswith(f".{a}") for a in allowed)


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

        automod_reason = await self._check_automod(message)
        if automod_reason:
            await self._automod_violation(message, automod_reason)
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

    # ---- Automod: Wortfilter, Großbuchstaben, Emoji-Spam, Link-Filter ----

    async def _check_automod(self, message: discord.Message) -> str | None:
        guild_id = message.guild.id
        content = message.content

        banned_raw = self.db.get(guild_id, "automod_banned_words")
        if banned_raw:
            words = [w.strip().lower() for w in banned_raw.split(",") if w.strip()]
            content_lower = content.lower()
            if any(w in content_lower for w in words):
                return "verbotenes Wort"

        if self.db.get_bool(guild_id, "automod_caps") and caps_ratio(content) > CAPS_RATIO:
            return "zu viele Großbuchstaben"

        emoji_limit = self.db.get_int(guild_id, "automod_emoji_limit")
        if emoji_limit and len(EMOJI_RE.findall(content)) > emoji_limit:
            return "Emoji-Spam"

        if self.db.get_bool(guild_id, "automod_link_filter"):
            allowed = {d.strip().lower() for d in (self.db.get(guild_id, "automod_allowed_domains") or "").split(",") if d.strip()}
            for domain in LINK_RE.findall(content):
                if not domain_allowed(domain, allowed):
                    return "nicht erlaubter Link"

        return None

    async def _automod_violation(self, message: discord.Message, reason: str) -> None:
        self._own_deletes.add(message.id)
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await message.channel.send(
                f"{message.author.mention}, deine Nachricht wurde entfernt ({reason}).", delete_after=15
            )
        except discord.HTTPException:
            pass
        await self.add_warning(message.guild, message.author, self.bot.user, f"Automod: {reason}")

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
        # Grund kommt aus dem Audit-Log, damit /ban (und manuelle Banns über Discord) nur einen
        # Eintrag erzeugen statt zwei.
        reason = None
        if guild.me.guild_permissions.view_audit_log:
            try:
                async for entry in guild.audit_logs(action=discord.AuditLogAction.ban, limit=5):
                    if entry.target and entry.target.id == user.id:
                        reason = entry.reason
                        break
            except discord.HTTPException:
                pass
        desc = f"{user.mention} ({user})" + (f"\n{reason}" if reason else "")
        await self.log(guild, discord.Embed(title="🔨 Gebannt", colour=discord.Colour.dark_red(), description=desc))

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

    # ---- Verwarnungen ----

    @app_commands.command(name="warn", description="Mitglied verwarnen, mit Grund")
    @app_commands.describe(grund="Wird im Log und in der Verwarnung selbst vermerkt")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, mitglied: discord.Member, grund: str):
        if mitglied.bot:
            raise UserError("Bots kann ich nicht verwarnen.")
        count = await self.add_warning(interaction.guild, mitglied, interaction.user, grund)
        await interaction.response.send_message(
            f"{mitglied.mention} verwarnt (Nr. {count}). Grund: {grund}", ephemeral=True
        )

    async def add_warning(
        self, guild: discord.Guild, member: discord.Member, moderator: discord.abc.User, reason: str
    ) -> int:
        """Verwarnung eintragen, Betroffene/n per DM informieren, loggen, und bei Erreichen der
        Schwelle automatisch bestrafen. Von `/warn` UND von den automatischen Automod-Verstößen
        unten genutzt (Moderator dort: der Bot selbst)."""
        await self.db.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created) VALUES (?, ?, ?, ?, ?)",
            (guild.id, member.id, moderator.id, reason, time.time()),
        )
        count = len(await self.db.fetchall(
            "SELECT id FROM warnings WHERE guild_id=? AND user_id=?", (guild.id, member.id)
        ))
        try:
            await member.send(f"Du wurdest auf **{guild.name}** verwarnt (Nr. {count}).\nGrund: {reason}")
        except discord.HTTPException:
            pass
        await self.log(guild, discord.Embed(
            title=f"⚠️ Verwarnung Nr. {count}", colour=discord.Colour.yellow(),
            description=f"{member.mention} ({member})\nVon {moderator.mention}\nGrund: {reason}",
        ))
        threshold = self.db.get_int(guild.id, "warn_threshold")
        action = self.db.get(guild.id, "warn_action")
        if threshold and action and count == threshold:
            await self._auto_punish(guild, member, action, count)
        return count

    async def _auto_punish(self, guild: discord.Guild, member: discord.Member, action: str, count: int) -> None:
        me = guild.me
        if member.top_role >= me.top_role or member == guild.owner:
            return
        reason = f"Automatisch: {count} Verwarnungen"
        try:
            if action == "timeout" and me.guild_permissions.moderate_members:
                await member.timeout(timedelta(hours=1), reason=reason)
            elif action == "kick" and me.guild_permissions.kick_members:
                await member.kick(reason=reason)
            elif action == "ban" and me.guild_permissions.ban_members:
                await member.ban(reason=reason, delete_message_seconds=0)
            else:
                return
        except discord.HTTPException as ex:
            log.warning("Automatische Strafe %s: %r", member.id, ex)
            return
        if action != "ban":  # Ban loggt sich selbst über on_member_ban
            await self.log(guild, discord.Embed(
                title=f"🚨 Automatisch: {action}", colour=discord.Colour.dark_red(),
                description=f"{member.mention} ({member}) nach {count} Verwarnungen",
            ))

    @app_commands.command(name="warnungen", description="Verwarnungen eines Mitglieds anzeigen")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warnings_list(self, interaction: discord.Interaction, mitglied: discord.Member):
        rows = await self.db.fetchall(
            "SELECT id, moderator_id, reason, created FROM warnings WHERE guild_id=? AND user_id=? ORDER BY created DESC",
            (interaction.guild_id, mitglied.id),
        )
        if not rows:
            return await interaction.response.send_message(f"{mitglied.mention} hat keine Verwarnungen.", ephemeral=True)
        lines = []
        for r in rows:
            mod = interaction.guild.get_member(r["moderator_id"])
            when = discord.utils.format_dt(datetime.fromtimestamp(r["created"], tz=timezone.utc), "R")
            lines.append(f"**#{r['id']}** {when} – {clip(r['reason'], 200)} "
                        f"(von {mod.mention if mod else r['moderator_id']})")
        e = discord.Embed(title=f"⚠️ {len(rows)} Verwarnung(en): {mitglied}", colour=discord.Colour.yellow(),
                          description="\n".join(lines)[:4000])
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="warnung-loeschen", description="Einzelne Verwarnung per ID löschen (siehe /warnungen)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn_delete(self, interaction: discord.Interaction, warnung_id: int):
        row = await self.db.fetchone(
            "SELECT id FROM warnings WHERE id=? AND guild_id=?", (warnung_id, interaction.guild_id)
        )
        if not row:
            raise UserError(f"Keine Verwarnung mit der ID {warnung_id} gefunden.")
        await self.db.execute("DELETE FROM warnings WHERE id=?", (warnung_id,))
        await interaction.response.send_message(f"Verwarnung #{warnung_id} gelöscht.", ephemeral=True)

    # ---- Kick/Ban ----

    @app_commands.command(name="kick", description="Mitglied vom Server entfernen (kann wieder beitreten)")
    @app_commands.describe(grund="Wird im Log vermerkt")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, mitglied: discord.Member, grund: str | None = None):
        if mitglied.top_role >= interaction.guild.me.top_role or mitglied == interaction.guild.owner:
            return await interaction.response.send_message("Dieses Mitglied kann ich nicht entfernen.", ephemeral=True)
        try:
            await mitglied.send(f"Du wurdest von **{interaction.guild.name}** entfernt."
                                + (f"\nGrund: {grund}" if grund else ""))
        except discord.HTTPException:
            pass
        await mitglied.kick(reason=f"{interaction.user}: {grund or 'ohne Grund'}")
        await interaction.response.send_message(f"{mitglied.mention} wurde entfernt.", ephemeral=True)
        await self.log(interaction.guild, discord.Embed(
            title="👢 Kick", colour=discord.Colour.orange(),
            description=f"{mitglied.mention} ({mitglied})\nVon {interaction.user.mention}"
                        + (f"\nGrund: {grund}" if grund else ""),
        ))

    @app_commands.command(name="ban", description="Mitglied bannen")
    @app_commands.describe(grund="Wird im Log vermerkt",
                           nachrichten_tage="Nachrichten der letzten X Tage mitlöschen (0–7)")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    async def ban(self, interaction: discord.Interaction, mitglied: discord.Member, grund: str | None = None,
                  nachrichten_tage: app_commands.Range[int, 0, 7] = 0):
        if mitglied.top_role >= interaction.guild.me.top_role or mitglied == interaction.guild.owner:
            return await interaction.response.send_message("Dieses Mitglied kann ich nicht bannen.", ephemeral=True)
        try:
            await mitglied.send(f"Du wurdest von **{interaction.guild.name}** gebannt."
                                + (f"\nGrund: {grund}" if grund else ""))
        except discord.HTTPException:
            pass
        await mitglied.ban(reason=f"{interaction.user}: {grund or 'ohne Grund'}",
                           delete_message_seconds=nachrichten_tage * 86400)
        await interaction.response.send_message(f"{mitglied.mention} wurde gebannt.", ephemeral=True)

    @app_commands.command(name="unban", description="Bann aufheben (User-ID, siehe /aufraeumen bericht oder Bann-Liste)")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, grund: str | None = None):
        if not user_id.isdigit():
            raise UserError("Das ist keine gültige User-ID.")
        user = discord.Object(id=int(user_id))
        try:
            await interaction.guild.unban(user, reason=f"{interaction.user}: {grund or 'ohne Grund'}")
        except discord.NotFound:
            raise UserError("Dieser User ist nicht gebannt.")
        await interaction.response.send_message(f"Bann für `{user_id}` aufgehoben.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
