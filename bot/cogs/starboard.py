"""Starboard: Nachrichten ab einer einstellbaren Anzahl ⭐-Reaktionen landen als Embed in einem
Highlight-Kanal. Der Zähler zieht die eigene Reaktion des Autors ab (sich selbst starren zählt
nicht), und folgt Hinzufügen/Entfernen von Sternen live über die `raw`-Reaktions-Events (damit es
auch für Nachrichten funktioniert, die nicht mehr im Discord.py-Cache liegen).
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

STAR_EMOJI = "⭐"
DEFAULT_THRESHOLD = 3


def build_star_embed(message: discord.Message, count: int) -> discord.Embed:
    e = discord.Embed(description=message.content or "", colour=discord.Colour.gold(), timestamp=message.created_at)
    e.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            e.set_image(url=attachment.url)
            break
    e.add_field(name="Original", value=f"[zur Nachricht]({message.jump_url})", inline=False)
    return e


class Starboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle(payload)

    async def _handle(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id or str(payload.emoji) != STAR_EMOJI:
            return
        board_channel_id = self.db.get_int(payload.guild_id, "starboard_channel")
        if not board_channel_id or payload.channel_id == board_channel_id:
            return  # kein Starboard eingerichtet, oder Reaktion im Starboard-Kanal selbst
        source_channel = self.bot.get_channel(payload.channel_id)
        if source_channel is None:
            return
        try:
            message = await source_channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        count = await self._star_count(message)
        await self._sync(message, count, board_channel_id)

    async def _star_count(self, message: discord.Message) -> int:
        for reaction in message.reactions:
            if str(reaction.emoji) == STAR_EMOJI:
                users = [u async for u in reaction.users()]
                return sum(1 for u in users if u.id != message.author.id)
        return 0

    async def _sync(self, message: discord.Message, count: int, board_channel_id: int) -> None:
        guild = message.guild
        board_channel = guild.get_channel(board_channel_id)
        if not board_channel:
            return
        threshold = self.db.get_int(guild.id, "starboard_threshold") or DEFAULT_THRESHOLD
        row = await self.db.fetchone("SELECT star_message_id FROM starboard WHERE message_id=?", (message.id,))

        if count < threshold:
            if row and row["star_message_id"]:
                try:
                    star_msg = await board_channel.fetch_message(row["star_message_id"])
                    await star_msg.delete()
                except discord.HTTPException:
                    pass
                await self.db.execute("DELETE FROM starboard WHERE message_id=?", (message.id,))
            return

        content = f"{STAR_EMOJI} **{count}** | {message.channel.mention}"
        embed = build_star_embed(message, count)
        if row and row["star_message_id"]:
            try:
                star_msg = await board_channel.fetch_message(row["star_message_id"])
                await star_msg.edit(content=content, embed=embed)
                return
            except discord.HTTPException:
                pass
        try:
            star_msg = await board_channel.send(content=content, embed=embed)
        except discord.HTTPException as ex:
            log.warning("Starboard-Post in %s: %r", guild.id, ex)
            return
        await self.db.execute(
            "INSERT INTO starboard (guild_id, message_id, star_message_id) VALUES (?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET star_message_id=excluded.star_message_id",
            (guild.id, message.id, star_msg.id),
        )


async def setup(bot):
    await bot.add_cog(Starboard(bot))
