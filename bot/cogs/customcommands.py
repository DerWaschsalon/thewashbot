"""Eigene Befehle: `!name` im Chat antwortet mit einem hinterlegten Text, Platzhalter `{user}`
(Erwähnung) und `{server}` (Servername) werden ersetzt. Unabhängig vom Slash-Befehls-System —
reine Text-Trigger über `on_message`.
"""

from __future__ import annotations

import re

import discord
from discord import app_commands
from discord.ext import commands

from ..errors import UserError

NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def render(response: str, member: discord.Member, guild: discord.Guild) -> str:
    return response.replace("{user}", member.mention).replace("{server}", guild.name)


class CustomCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content.startswith("!"):
            return
        name = message.content[1:].split()[0].lower() if message.content[1:].split() else ""
        if not name:
            return
        row = await self.db.fetchone(
            "SELECT response FROM custom_commands WHERE guild_id=? AND name=?", (message.guild.id, name)
        )
        if not row:
            return
        try:
            await message.channel.send(render(row["response"], message.author, message.guild))
        except discord.HTTPException:
            pass

    @app_commands.command(name="befehl-erstellen", description="Eigenen Befehl !name anlegen oder überschreiben")
    @app_commands.describe(
        name="Name ohne „!“, nur a-z 0-9 _ - (höchstens 32 Zeichen)",
        antwort="Antworttext. Platzhalter: {user} (Erwähnung), {server}",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def create_command(self, interaction: discord.Interaction, name: str, antwort: str):
        name = name.strip().lower().lstrip("!")
        if not NAME_RE.match(name):
            raise UserError("Name darf nur a-z, 0-9, „_“ und „-“ enthalten (höchstens 32 Zeichen).")
        await self.db.execute(
            "INSERT INTO custom_commands (guild_id, name, response) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, name) DO UPDATE SET response=excluded.response",
            (interaction.guild_id, name, antwort),
        )
        await interaction.response.send_message(f"`!{name}` gespeichert.", ephemeral=True)

    @app_commands.command(name="befehl-loeschen", description="Eigenen Befehl wieder löschen")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def delete_command(self, interaction: discord.Interaction, name: str):
        name = name.strip().lower().lstrip("!")
        deleted = await self.db.fetchone(
            "SELECT id FROM custom_commands WHERE guild_id=? AND name=?", (interaction.guild_id, name)
        )
        if not deleted:
            raise UserError(f"Es gibt keinen Befehl `!{name}`.")
        await self.db.execute(
            "DELETE FROM custom_commands WHERE guild_id=? AND name=?", (interaction.guild_id, name)
        )
        await interaction.response.send_message(f"`!{name}` gelöscht.", ephemeral=True)

    @app_commands.command(name="befehle", description="Alle eigenen Befehle dieses Servers anzeigen")
    @app_commands.guild_only()
    async def list_commands(self, interaction: discord.Interaction):
        rows = await self.db.fetchall(
            "SELECT name FROM custom_commands WHERE guild_id=? ORDER BY name", (interaction.guild_id,)
        )
        if not rows:
            return await interaction.response.send_message("Es gibt noch keine eigenen Befehle.", ephemeral=True)
        names = ", ".join(f"`!{r['name']}`" for r in rows)
        await interaction.response.send_message(names[:2000], ephemeral=True)


async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
