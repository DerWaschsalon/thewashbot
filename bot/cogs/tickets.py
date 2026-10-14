"""Tickets: Panel mit Buttons (Frage, Meldung, Bewerbung) → privater Thread mit Team-Rolle.

Team-Rolle wird mit `/setup tickets` festgelegt (oder im Klick-Panel), das Panel selbst mit
`/ticket-panel` in den gewünschten Support-Kanal gepostet. Die Buttons im Panel und der
„Schließen“-Button im Thread sind wie bei `/rollen-panel` über feste `custom_id`s + einen
`on_interaction`-Listener verdrahtet, überleben also Neustarts ohne `bot.add_view`.
"""

from __future__ import annotations

import io
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..errors import UserError

log = logging.getLogger(__name__)
PANEL_PREFIX = "ticket:open:"
CLOSE_ID = "ticket:close"

KINDS = {
    "frage": ("❓", "Frage", "Frage"),
    "meldung": ("⚠️", "Meldung", "Was melden?"),
    "bewerbung": ("📋", "Bewerbung", "Kurz zu dir"),
}


def team_roles(db, guild: discord.Guild) -> list[discord.Role]:
    return [r for rid in db.get_ids(guild.id, "ticket_team_roles") if (r := guild.get_role(rid))]


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for key, (emoji, label, _) in KINDS.items():
            self.add_item(discord.ui.Button(
                label=label, emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=f"{PANEL_PREFIX}{key}",
            ))


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Schließen", emoji="🔒", style=discord.ButtonStyle.danger, custom_id=CLOSE_ID,
        ))


class TicketModal(discord.ui.Modal):
    def __init__(self, kind: str):
        emoji, label, field_label = KINDS[kind]
        super().__init__(title=f"{emoji} {label}")
        self.kind = kind
        self.text = discord.ui.TextInput(label=field_label, style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        cog: Tickets = interaction.client.get_cog("Tickets")
        await cog.create_ticket(interaction, self.kind, str(self.text.value))


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    async def create_ticket(self, interaction: discord.Interaction, kind: str, text: str) -> None:
        guild = interaction.guild
        channel = interaction.channel
        roles = team_roles(self.db, guild)
        if not roles:
            return await interaction.response.send_message(
                "Noch keine Team-Rolle für Tickets eingerichtet (`/setup tickets`).", ephemeral=True
            )
        if not channel.permissions_for(guild.me).create_private_threads:
            return await interaction.response.send_message(
                "Mir fehlt das Recht „Private Threads erstellen“ in diesem Kanal.", ephemeral=True
            )
        emoji, label, _ = KINDS[kind]
        await interaction.response.defer(ephemeral=True, thinking=True)
        thread = await channel.create_thread(
            name=f"{emoji} {label} – {interaction.user.display_name}"[:100],
            type=discord.ChannelType.private_thread, invitable=False,
            reason=f"Ticket von {interaction.user}",
        )
        await thread.add_user(interaction.user)
        seen_members: set[int] = set()
        for role in roles:
            for member in role.members:
                if member.id in seen_members:
                    continue
                seen_members.add(member.id)
                try:
                    await thread.add_user(member)
                except discord.HTTPException as e:
                    log.warning("Ticket %s: Team-Mitglied %s nicht hinzugefügt: %r", thread.id, member.id, e)

        await self.db.execute(
            "INSERT INTO tickets (channel_id, guild_id, user_id, kind, created) VALUES (?, ?, ?, ?, ?)",
            (thread.id, guild.id, interaction.user.id, kind, time.time()),
        )

        e = discord.Embed(title=f"{emoji} {label}", colour=discord.Colour.blurple(), description=text)
        e.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        pings = " ".join(r.mention for r in roles)
        await thread.send(
            content=f"{interaction.user.mention} {pings}", embed=e, view=TicketCloseView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
        await interaction.followup.send(f"Ticket erstellt: {thread.mention}", ephemeral=True)

    async def close_ticket(self, interaction: discord.Interaction) -> None:
        thread = interaction.channel
        guild = interaction.guild
        row = await self.db.fetchone("SELECT * FROM tickets WHERE channel_id=?", (thread.id,))
        if not row:
            return await interaction.response.send_message("Das ist kein Ticket (mehr).", ephemeral=True)
        roles = team_roles(self.db, guild)
        is_team = isinstance(interaction.user, discord.Member) and any(r in interaction.user.roles for r in roles)
        if not (is_team or interaction.user.id == row["user_id"] or interaction.user.guild_permissions.manage_guild):
            return await interaction.response.send_message(
                "Nur das Team oder du selbst können dieses Ticket schließen.", ephemeral=True
            )
        await interaction.response.send_message("🔒 Wird geschlossen …")

        lines = []
        async for m in thread.history(limit=500, oldest_first=True):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.created_at.timestamp()))
            lines.append(f"[{when}] {m.author}: {m.content}")
        transcript = "\n".join(lines) or "(keine Nachrichten)"

        log_channel_id = self.db.get_int(guild.id, "log_channel")
        log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
        if log_channel:
            opener = guild.get_member(row["user_id"])
            summary = discord.Embed(
                title=f"🎫 Ticket geschlossen: {thread.name}", colour=discord.Colour.dark_grey(),
                description=f"Von {opener.mention if opener else row['user_id']}, "
                            f"geschlossen von {interaction.user.mention}",
            )
            try:
                await log_channel.send(
                    embed=summary,
                    file=discord.File(io.BytesIO(transcript.encode()), filename=f"ticket-{thread.id}.txt"),
                )
            except discord.HTTPException as e:
                log.warning("Ticket-Verlauf %s: %r", thread.id, e)

        await self.db.execute("UPDATE tickets SET closed=? WHERE channel_id=?", (time.time(), thread.id))
        await thread.edit(archived=True, locked=True, reason=f"Geschlossen von {interaction.user}")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if custom_id.startswith(PANEL_PREFIX):
            kind = custom_id[len(PANEL_PREFIX):]
            if kind in KINDS:
                await interaction.response.send_modal(TicketModal(kind))
        elif custom_id == CLOSE_ID:
            await self.close_ticket(interaction)

    @app_commands.command(name="ticket-panel", description="Panel posten, über das Mitglieder Tickets eröffnen können")
    @app_commands.describe(titel="Überschrift", text="Beschreibung unter der Überschrift")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def panel(
        self, interaction: discord.Interaction, titel: str = "Support",
        text: str = "Klick auf einen Button, um ein Ticket zu eröffnen.",
    ):
        if not team_roles(self.db, interaction.guild):
            raise UserError("Erst eine Team-Rolle einrichten: `/setup tickets rolle:`.")
        e = discord.Embed(title=titel, description=text.replace("\\n", "\n"), colour=discord.Colour.blurple())
        e.add_field(name="Optionen", value="\n".join(f"{emoji} {label}" for emoji, label, _ in KINDS.values()))
        await interaction.channel.send(embed=e, view=TicketPanelView())
        await interaction.response.send_message("Panel erstellt.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Tickets(bot))
