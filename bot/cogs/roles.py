"""Willkommen, Autorolle und Rollen-Panels (Rollen per Button selbst nehmen und abgeben).

Die Buttons eines Panels tragen die Rollen-ID in ihrer custom_id (`rp:<id>`). Sie werden über
`on_interaction` beantwortet und funktionieren deshalb auch nach einem Neustart weiter, ohne dass
der Bot sich Panels merken muss.
"""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from ..errors import UserError
from .settings import DEFAULT_WELCOME

log = logging.getLogger(__name__)
PANEL_PREFIX = "rp:"


def role_ok(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return not role.managed and not role.is_default() and role < me.top_role and me.guild_permissions.manage_roles


class PanelView(discord.ui.View):
    def __init__(self, roles: list[discord.Role], emojis: dict[int, str]):
        super().__init__(timeout=None)
        for role in roles:
            self.add_item(discord.ui.Button(
                label=role.name[:80],
                emoji=emojis.get(role.id),
                style=discord.ButtonStyle.secondary,
                custom_id=f"{PANEL_PREFIX}{role.id}",
            ))


class Roles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    # ---- Willkommen + Autorolle ----

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        # Autorolle gibt's sofort beim Beitritt (z. B. "Neuling"), unabhängig von der
        # Verifizierung — die vergibt bei Bestätigung ihre eigene Rolle (verify_role).
        role_id = self.db.get_int(guild.id, "autorole")
        if role_id and not member.bot:
            role = guild.get_role(role_id)
            if role and role_ok(guild, role):
                try:
                    await member.add_roles(role, reason="Autorolle")
                except discord.HTTPException as e:
                    log.warning("Autorolle %s: %r", guild.id, e)

        channel_id = self.db.get_int(guild.id, "welcome_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not channel or member.bot:
            return
        text = self.db.get(guild.id, "welcome_text", DEFAULT_WELCOME)
        text = (text.replace("{user}", member.mention)
                    .replace("{name}", discord.utils.escape_markdown(member.display_name))
                    .replace("{server}", guild.name)
                    .replace("{anzahl}", str(guild.member_count)))
        embed = discord.Embed(description=text, colour=discord.Colour.green())
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.set_footer(text=f"Mitglied Nr. {guild.member_count}")
        try:
            await channel.send(content=member.mention, embed=embed,
                               allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException as e:
            log.warning("Willkommen %s: %r", guild.id, e)

    # ---- Rollen-Panel ----

    @app_commands.command(name="rollen-panel", description="Nachricht mit Buttons, über die man sich Rollen selbst gibt")
    @app_commands.describe(
        rollen="Rollen erwähnen, optional mit Emoji davor, z. B. „🎮 @Gamer 🎵 @Musik“",
        titel="Überschrift",
        text="Beschreibung unter der Überschrift",
    )
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def panel(self, interaction: discord.Interaction, rollen: str,
                    titel: str = "Rollen", text: str = "Klick auf einen Button, um die Rolle zu nehmen oder abzugeben."):
        guild = interaction.guild
        roles: list[discord.Role] = []
        emojis: dict[int, str] = {}
        # „<emoji> <@&id>“-Paare; Emoji ist optional (Unicode oder <:name:id>)
        for m in re.finditer(r"(?:(<a?:\w+:\d+>|[^\x00-\x7F\s]+)\s*)?<@&(\d+)>", rollen):
            role = guild.get_role(int(m.group(2)))
            if not role or role in roles:
                continue
            if not role_ok(guild, role):
                raise UserError(f"{role.mention} kann ich nicht vergeben. Meine Rolle muss darüber stehen, "
                                "und ich brauche „Rollen verwalten“.")
            roles.append(role)
            if m.group(1):
                emojis[role.id] = m.group(1)
        if not roles:
            raise UserError("Keine Rollen gefunden. Erwähne sie mit @, z. B. `🎮 @Gamer 🎵 @Musik`.")
        if len(roles) > 25:
            raise UserError("Höchstens 25 Rollen pro Panel.")
        embed = discord.Embed(title=titel, description=text.replace("\\n", "\n"), colour=discord.Colour.blurple())
        embed.add_field(name="Rollen", value="\n".join(
            f"{emojis.get(r.id, '•')} {r.mention}" for r in roles))
        await interaction.channel.send(embed=embed, view=PanelView(roles, emojis))
        await interaction.response.send_message("Panel erstellt.", ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith(PANEL_PREFIX) or not interaction.guild:
            return
        role = interaction.guild.get_role(int(custom_id[len(PANEL_PREFIX):]))
        if not role:
            return await interaction.response.send_message("Diese Rolle gibt es nicht mehr.", ephemeral=True)
        if not role_ok(interaction.guild, role):
            return await interaction.response.send_message(
                "Ich kann diese Rolle gerade nicht vergeben. Sag einem Admin Bescheid.", ephemeral=True)
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Rollen-Panel")
                text = f"{role.mention} entfernt."
            else:
                await member.add_roles(role, reason="Rollen-Panel")
                text = f"Du hast jetzt {role.mention}."
        except discord.HTTPException:
            text = "Das hat nicht geklappt."
        await interaction.response.send_message(text, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Roles(bot))
