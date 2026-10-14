"""Temp-Sprachkanäle („Join to create“).

Wer den Hub-Kanal (/setup temp-voice) betritt, bekommt einen eigenen Sprachkanal in derselben
Kategorie und wird hineinverschoben. Der Besitzer darf ihn umbenennen, begrenzen und sperren
(per /vc oder direkt in Discord, er hat „Kanal verwalten“). Ist der Kanal leer, wird er gelöscht.
Leere Reste nach einem Neustart räumt der Bot beim Start auf.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..errors import UserError

log = logging.getLogger(__name__)


@app_commands.guild_only()
class TempVoice(commands.GroupCog, group_name="vc", group_description="Deinen eigenen Sprachkanal verwalten"):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self._cleaned = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._cleaned:
            return
        self._cleaned = True
        for channel_id, guild_id, _ in await self.db.temp_channels():
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                await self.db.remove_temp_channel(channel_id)
            elif not channel.members:
                await self._delete(channel)

    async def _delete(self, channel: discord.VoiceChannel) -> None:
        try:
            await channel.delete(reason="Temp-Voice leer")
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            log.warning("Temp-Voice %s löschen: %r", channel.id, e)
            return
        await self.db.remove_temp_channel(channel.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        guild = member.guild
        hub_id = self.db.get_int(guild.id, "tempvoice_hub")

        if hub_id and after.channel and after.channel.id == hub_id:
            hub = after.channel
            overwrites = dict(hub.category.overwrites if hub.category else {})
            overwrites[member] = discord.PermissionOverwrite(
                manage_channels=True, move_members=True, connect=True, speak=True)
            overwrites[guild.me] = discord.PermissionOverwrite(
                manage_channels=True, connect=True, move_members=True, view_channel=True)
            try:
                channel = await guild.create_voice_channel(
                    f"🔊 {member.display_name}"[:100], category=hub.category,
                    overwrites=overwrites, bitrate=min(guild.bitrate_limit, 96000),
                    reason=f"Temp-Voice für {member}")
                await self.db.add_temp_channel(channel.id, guild.id, member.id)
                await member.move_to(channel)
            except discord.HTTPException as e:
                log.warning("Temp-Voice anlegen %s: %r", guild.id, e)

        if before.channel and before.channel != after.channel and not before.channel.members:
            if await self.db.temp_channel_owner(before.channel.id) is not None:
                await self._delete(before.channel)

    async def _own_channel(self, interaction: discord.Interaction) -> discord.VoiceChannel:
        voice = interaction.user.voice
        if not voice or not voice.channel:
            raise UserError("Du bist in keinem Sprachkanal.")
        owner = await self.db.temp_channel_owner(voice.channel.id)
        if owner is None:
            raise UserError("Das ist kein Temp-Kanal.")
        if owner != interaction.user.id:
            raise UserError(f"Der Kanal gehört <@{owner}>.")
        return voice.channel

    @app_commands.command(name="name", description="Deinen Kanal umbenennen")
    async def rename(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 90]):
        channel = await self._own_channel(interaction)
        await channel.edit(name=name)
        await interaction.response.send_message(f"Heißt jetzt **{discord.utils.escape_markdown(name)}**.", ephemeral=True)

    @app_commands.command(name="limit", description="Höchstzahl an Leuten (0 = unbegrenzt)")
    async def limit(self, interaction: discord.Interaction, anzahl: app_commands.Range[int, 0, 99]):
        channel = await self._own_channel(interaction)
        await channel.edit(user_limit=anzahl)
        await interaction.response.send_message(f"Limit: {anzahl or 'keins'}.", ephemeral=True)

    @app_commands.command(name="sperren", description="Niemand Neues kommt rein (außer Leuten, die du einlädst)")
    async def lock(self, interaction: discord.Interaction):
        channel = await self._own_channel(interaction)
        await channel.set_permissions(interaction.guild.default_role, connect=False)
        await interaction.response.send_message("🔒 Gesperrt. Wer drin ist, bleibt drin. `/vc einladen` lässt Leute rein.", ephemeral=True)

    @app_commands.command(name="freigeben", description="Kanal wieder für alle öffnen")
    async def unlock(self, interaction: discord.Interaction):
        channel = await self._own_channel(interaction)
        await channel.set_permissions(interaction.guild.default_role, connect=None)
        await interaction.response.send_message("🔓 Offen.", ephemeral=True)

    @app_commands.command(name="einladen", description="Jemanden in deinen gesperrten Kanal lassen")
    async def invite(self, interaction: discord.Interaction, mitglied: discord.Member):
        channel = await self._own_channel(interaction)
        await channel.set_permissions(mitglied, connect=True, view_channel=True)
        await interaction.response.send_message(f"{mitglied.mention} darf jetzt rein.", ephemeral=True)

    @app_commands.command(name="uebernehmen", description="Kanal übernehmen, wenn der Besitzer weg ist")
    async def claim(self, interaction: discord.Interaction):
        voice = interaction.user.voice
        if not voice or not voice.channel:
            raise UserError("Du bist in keinem Sprachkanal.")
        channel = voice.channel
        owner_id = await self.db.temp_channel_owner(channel.id)
        if owner_id is None:
            raise UserError("Das ist kein Temp-Kanal.")
        if any(m.id == owner_id for m in channel.members):
            raise UserError(f"<@{owner_id}> ist noch da.")
        old = interaction.guild.get_member(owner_id)
        if old:
            await channel.set_permissions(old, overwrite=None)
        await channel.set_permissions(interaction.user, manage_channels=True, move_members=True, connect=True)
        await self.db.set_temp_channel_owner(channel.id, interaction.user.id)
        await interaction.response.send_message("Der Kanal gehört jetzt dir.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(TempVoice(bot))
