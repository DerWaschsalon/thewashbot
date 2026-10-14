"""Aufräumen: alte Nachrichten löschen, alte Bots entfernen, Bericht über den Server.

Alles, was etwas löscht, zeigt erst eine Vorschau mit Bestätigungs-Button. Nur wer den Befehl
ausgelöst hat, kann bestätigen.

Hinweis zu Discord: Nachrichten jünger als 14 Tage löscht der Bot in Blöcken zu 100. Ältere
müssen einzeln gelöscht werden, das dauert etwa eine Sekunde pro Nachricht.
"""

from __future__ import annotations

import io
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ..errors import UserError

log = logging.getLogger(__name__)
SCAN_LIMIT = 5000


class Confirm(discord.ui.View):
    def __init__(self, author_id: int, label: str):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.confirmed: bool | None = None
        self.confirm.label = label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Das ist nicht deine Abfrage.", ephemeral=True)
            return False
        return True

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _):
        self.confirmed = True
        await interaction.response.edit_message(content="⏳ Läuft …", view=None, embed=None)
        self.stop()

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _):
        self.confirmed = False
        await interaction.response.edit_message(content="Abgebrochen.", view=None, embed=None)
        self.stop()


class BotSelect(discord.ui.Select):
    def __init__(self, bots: list[discord.Member]):
        super().__init__(
            placeholder="Bots auswählen, die rausfliegen sollen",
            min_values=1, max_values=len(bots),
            options=[discord.SelectOption(
                label=str(b.display_name)[:100], value=str(b.id),
                description=f"Seit {b.joined_at:%d.%m.%Y}" if b.joined_at else None,
            ) for b in bots],
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected = [int(v) for v in self.values]
        await interaction.response.defer()


class BotRemoveView(Confirm):
    def __init__(self, author_id: int, bots: list[discord.Member]):
        super().__init__(author_id, "Ausgewählte entfernen")
        self.selected: list[int] = []
        self.add_item(BotSelect(bots))

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _):
        if not self.selected:
            return await interaction.response.send_message("Erst Bots auswählen.", ephemeral=True)
        self.confirmed = True
        await interaction.response.edit_message(content="⏳ Läuft …", view=None, embed=None)
        self.stop()


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class Cleanup(commands.GroupCog, group_name="aufraeumen", group_description="Server aufräumen"):
    def __init__(self, bot):
        self.bot = bot

    # ---- Nachrichten ----

    @app_commands.command(name="nachrichten", description="Nachrichten in einem Kanal löschen (mit Vorschau)")
    @app_commands.describe(
        kanal="Welcher Kanal",
        aelter_als_tage="Nur Nachrichten, die älter sind als so viele Tage (0 = alle)",
        anzahl="Höchstens so viele löschen",
        von="Nur Nachrichten dieses Mitglieds oder Bots",
        nur_bots="Nur Nachrichten von Bots (z. B. alte Musikbot-Ausgaben)",
        angepinnte_behalten="Angepinnte Nachrichten nicht löschen",
    )
    async def messages(
        self, interaction: discord.Interaction, kanal: discord.TextChannel,
        aelter_als_tage: app_commands.Range[int, 0, 3650] = 0,
        anzahl: app_commands.Range[int, 1, SCAN_LIMIT] = 100,
        von: discord.User | None = None,
        nur_bots: bool = False,
        angepinnte_behalten: bool = True,
    ):
        perms = kanal.permissions_for(interaction.guild.me)
        if not (perms.manage_messages and perms.read_message_history):
            raise UserError(f"In {kanal.mention} fehlen mir „Nachrichten verwalten“ und „Nachrichtenverlauf lesen“.")
        await interaction.response.defer(ephemeral=True, thinking=True)

        before = discord.utils.utcnow() - timedelta(days=aelter_als_tage) if aelter_als_tage else None

        def check(m: discord.Message) -> bool:
            if angepinnte_behalten and m.pinned:
                return False
            if von and m.author.id != von.id:
                return False
            if nur_bots and not m.author.bot:
                return False
            return True

        # Vorschau: zählen, was getroffen würde
        matches: list[discord.Message] = []
        async for m in kanal.history(limit=SCAN_LIMIT, before=before):
            if check(m):
                matches.append(m)
                if len(matches) >= anzahl:
                    break
        if not matches:
            return await interaction.followup.send("Keine passenden Nachrichten gefunden.", ephemeral=True)

        old = sum(1 for m in matches if discord.utils.utcnow() - m.created_at > timedelta(days=14))
        e = discord.Embed(title=f"🧹 {len(matches)} Nachrichten in #{kanal.name} löschen?", colour=discord.Colour.red())
        e.add_field(name="Zeitraum", value=f"{matches[-1].created_at:%d.%m.%Y} – {matches[0].created_at:%d.%m.%Y}")
        if old:
            e.add_field(name="Dauer", value=f"{old} davon älter als 14 Tage, das dauert etwa {max(1, old // 60)} Min.")
        e.set_footer(text="Gelöschte Nachrichten lassen sich nicht wiederherstellen.")
        view = Confirm(interaction.user.id, f"{len(matches)} löschen")
        await interaction.followup.send(embed=e, view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            return

        targets = {m.id for m in matches}
        self.bot.quiet_deletes |= targets
        deleted = await kanal.purge(
            limit=None, before=before, after=matches[-1].created_at - timedelta(seconds=1),
            check=lambda m: m.id in targets, reason=f"Aufräumen durch {interaction.user}",
        )
        self.bot.quiet_deletes -= targets
        await interaction.edit_original_response(content=f"✅ {len(deleted)} Nachrichten gelöscht.")

    # ---- Bots ----

    @app_commands.command(name="bots", description="Alte Bots anzeigen und entfernen")
    async def bots(self, interaction: discord.Interaction):
        guild = interaction.guild
        bots = sorted((m for m in guild.members if m.bot and m.id != self.bot.user.id),
                      key=lambda m: m.joined_at or discord.utils.utcnow())
        if not bots:
            raise UserError("Außer mir sind keine Bots auf dem Server.")
        me = guild.me
        lines = []
        kickable = []
        for b in bots:
            roles = ", ".join(r.name for r in b.roles if not r.is_default()) or "keine"
            ok = me.guild_permissions.kick_members and b.top_role < me.top_role
            lines.append(f"{'•' if ok else '🔒'} **{discord.utils.escape_markdown(b.display_name)}** "
                         f"– seit {b.joined_at:%d.%m.%Y} – Rollen: {roles}")
            if ok:
                kickable.append(b)
        e = discord.Embed(title=f"🤖 {len(bots)} Bots", description="\n".join(lines)[:4000], colour=discord.Colour.blurple())
        if len(kickable) < len(bots):
            e.set_footer(text="🔒 = steht über meiner Rolle. Rolle des Bots in den Servereinstellungen über diese ziehen "
                              "oder den Bot von Hand entfernen.")
        if not kickable:
            return await interaction.response.send_message(embed=e, ephemeral=True)
        view = BotRemoveView(interaction.user.id, kickable[:25])
        await interaction.response.send_message(embed=e, view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            return
        done = []
        for bot_id in view.selected:
            member = guild.get_member(bot_id)
            if member:
                try:
                    await member.kick(reason=f"Aufräumen durch {interaction.user}")
                    done.append(member.display_name)
                except discord.HTTPException as ex:
                    log.warning("Kick %s: %r", bot_id, ex)
        await interaction.edit_original_response(
            content=f"✅ Entfernt: {', '.join(done) or 'keiner'}.\n"
                    "Ihre automatischen Bot-Rollen verschwinden dabei mit. Selbst angelegte Rollen für diese Bots "
                    "bleiben, die siehst du in `/aufraeumen bericht`.")

    # ---- Bericht ----

    @app_commands.command(name="bericht", description="Übersicht über Kanäle, Aktivität und Rollen als Datei")
    async def report(self, interaction: discord.Interaction):
        guild = interaction.guild
        now = discord.utils.utcnow()
        out = [f"# Server-Bericht: {guild.name}", f"Stand: {now:%d.%m.%Y %H:%M} UTC · {guild.member_count} Mitglieder", ""]

        def last_activity(ch) -> str:
            last_id = getattr(ch, "last_message_id", None)
            if not last_id:
                return "nie / unbekannt"
            days = (now - discord.utils.snowflake_time(last_id)).days
            return f"vor {days} Tagen" + ("  ⚠️" if days > 90 else "")

        out.append("## Kanäle")
        for category, channels in guild.by_category():
            out.append(f"\n### {category.name if category else '(ohne Kategorie)'}")
            for ch in channels:
                kind = {discord.ChannelType.text: "Text", discord.ChannelType.voice: "Sprache",
                        discord.ChannelType.forum: "Forum", discord.ChannelType.news: "Ankündigung",
                        discord.ChannelType.stage_voice: "Bühne"}.get(ch.type, str(ch.type))
                line = f"- #{ch.name} ({kind})"
                if isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
                    line += f" – letzte Nachricht {last_activity(ch)}"
                if not ch.permissions_for(guild.default_role).view_channel:
                    line += " – privat"
                out.append(line)

        out.append("\n## Rollen")
        for role in reversed(guild.roles):
            if role.is_default():
                continue
            flags = []
            if role.managed:
                flags.append("gehört zu Bot/Integration")
            if role.permissions.administrator:
                flags.append("Administrator")
            if not role.members and not role.managed:
                flags.append("⚠️ niemand hat sie")
            out.append(f"- {role.name} – {len(role.members)} Mitglieder" + (f" – {', '.join(flags)}" if flags else ""))

        out.append("\n## Bots")
        for m in guild.members:
            if m.bot:
                out.append(f"- {m} – seit {m.joined_at:%d.%m.%Y}" if m.joined_at else f"- {m}")

        data = io.BytesIO("\n".join(out).encode())
        await interaction.response.send_message(
            "Hier der Bericht. ⚠️ markiert Kanäle ohne Nachricht seit 90 Tagen und leere Rollen.",
            file=discord.File(data, filename=f"server-bericht-{now:%Y-%m-%d}.md"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Cleanup(bot))
