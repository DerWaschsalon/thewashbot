"""Embed-Ersteller: `/embed-erstellen` öffnet ein Formular (Modal) und postet das fertige Embed
in einen Kanal — ohne JSON oder Drittanbieter-Tools.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..errors import UserError


def parse_colour(raw: str | None) -> discord.Colour:
    if not raw:
        return discord.Colour.blurple()
    text = raw.strip().lstrip("#")
    try:
        return discord.Colour(int(text, 16))
    except ValueError:
        raise UserError(f"„{raw}“ ist keine gültige Hex-Farbe, z. B. `#5865F2`.")


class EmbedModal(discord.ui.Modal, title="Embed erstellen"):
    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel
        self.titel = discord.ui.TextInput(label="Titel", max_length=256, required=False)
        self.beschreibung = discord.ui.TextInput(
            label="Beschreibung", style=discord.TextStyle.paragraph, max_length=4000, required=False,
        )
        self.farbe = discord.ui.TextInput(label="Farbe (Hex, z. B. #5865F2)", required=False, max_length=7)
        self.bild_url = discord.ui.TextInput(label="Bild-URL", required=False, max_length=500)
        for item in (self.titel, self.beschreibung, self.farbe, self.bild_url):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if not str(self.titel.value) and not str(self.beschreibung.value):
            return await interaction.response.send_message("Gib mindestens Titel oder Beschreibung an.", ephemeral=True)
        embed = discord.Embed(
            title=str(self.titel.value) or None,
            description=str(self.beschreibung.value) or None,
            colour=parse_colour(str(self.farbe.value) or None),
        )
        image_url = str(self.bild_url.value)
        if image_url:
            embed.set_image(url=image_url)
        if not self.channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"Ich darf in {self.channel.mention} nicht schreiben.", ephemeral=True)
        await self.channel.send(embed=embed)
        await interaction.response.send_message(f"Embed gepostet in {self.channel.mention}.", ephemeral=True)


class Embeds(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="embed-erstellen", description="Eigenes Embed per Formular erstellen und posten")
    @app_commands.describe(kanal="Kanal, in den das Embed gepostet wird (leer lassen = dieser Kanal)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def create_embed(self, interaction: discord.Interaction, kanal: discord.TextChannel | None = None):
        await interaction.response.send_modal(EmbedModal(kanal or interaction.channel))


async def setup(bot):
    await bot.add_cog(Embeds(bot))
