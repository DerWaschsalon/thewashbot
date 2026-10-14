"""Start: `python -m bot`. Einstellungen kommen aus Umgebungsvariablen (siehe .env.example)."""

from __future__ import annotations

import logging
import os
import sys

import discord
from discord.ext import commands

from .db import Database
from .errors import UserError

log = logging.getLogger("bot")

COGS = [
    "bot.cogs.settings",
    "bot.cogs.music",
    "bot.cogs.roles",
    "bot.cogs.moderation",
    "bot.cogs.tempvoice",
    "bot.cogs.cleanup",
    "bot.cogs.reminders",
]


class Bot(commands.Bot):
    def __init__(self, db: Database, *, sync: bool = True):
        intents = discord.Intents.default()
        intents.members = True          # Willkommen, Autorolle, Logs (privilegiert)
        intents.message_content = True  # Spamschutz, Lösch-/Bearbeitungs-Logs (privilegiert)
        intents.voice_states = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            activity=discord.Activity(type=discord.ActivityType.listening, name="/play"),
        )
        self.db = db
        self.quiet_deletes: set[int] = set()  # vom Bot selbst gelöscht, nicht einzeln loggen
        self._sync = sync

    async def setup_hook(self) -> None:
        await self.db.connect()
        for cog in COGS:
            await self.load_extension(cog)
        if not self._sync:
            return
        dev_guild = os.getenv("DEV_GUILD_ID", "").strip()
        if dev_guild.isdigit():
            # Sofort sichtbar auf einem Server (globale Befehle brauchen bis zu einer Stunde)
            guild = discord.Object(int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("%d Slash-Befehle registriert", len(synced))

    async def on_ready(self) -> None:
        log.info("Angemeldet als %s (%s), %d Server", self.user, self.user.id, len(self.guilds))
        setup_cog = self.get_cog("Setup")
        if setup_cog:
            await setup_cog.register_persistent_views()

    async def close(self) -> None:
        await super().close()
        await self.db.close()


async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    original = getattr(error, "original", None)
    if isinstance(original, UserError):
        text = str(original)
    elif isinstance(error, discord.app_commands.MissingPermissions):
        text = "Dafür fehlen dir die Rechte."
    elif isinstance(error, discord.app_commands.BotMissingPermissions):
        text = "Mir fehlen dafür Rechte: " + ", ".join(error.missing_permissions)
    elif isinstance(error, discord.app_commands.CheckFailure):
        text = str(error) or "Das geht hier nicht."
    else:
        log.exception("Fehler in /%s", interaction.command and interaction.command.qualified_name, exc_info=error)
        text = "Da ist etwas schiefgegangen. Details stehen im Log des Bots."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        sys.exit("DISCORD_TOKEN fehlt (siehe .env.example)")
    bot = Bot(Database(os.getenv("DB_PATH", "/data/bot.sqlite3")))
    bot.tree.on_error = on_app_command_error
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
