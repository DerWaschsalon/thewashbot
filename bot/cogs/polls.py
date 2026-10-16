"""Umfragen: native Discord-Polls per Slash-Befehl, kein eigenes Button/Reaktions-System nötig."""

from __future__ import annotations

import datetime

import discord
from discord import app_commands
from discord.ext import commands

from ..errors import UserError

MAX_ANSWER_LENGTH = 55


class Polls(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="umfrage", description="Native Discord-Umfrage erstellen")
    @app_commands.describe(
        frage="Die Frage",
        antworten="Antwortmöglichkeiten, durch | getrennt (2–10)",
        dauer_stunden="Wie lange die Umfrage läuft (Stunden, 1–168, Standard 24)",
        mehrfachauswahl="Mehrere Antworten pro Person erlauben (Standard: aus)",
    )
    @app_commands.guild_only()
    async def poll(
        self, interaction: discord.Interaction, frage: str, antworten: str,
        dauer_stunden: app_commands.Range[int, 1, 168] = 24, mehrfachauswahl: bool = False,
    ):
        options = parse_answers(antworten)
        survey = discord.Poll(
            question=frage, duration=datetime.timedelta(hours=dauer_stunden), multiple=mehrfachauswahl,
        )
        for option in options:
            survey.add_answer(text=option)
        await interaction.channel.send(poll=survey)
        await interaction.response.send_message("Umfrage gestartet.", ephemeral=True)


def parse_answers(raw: str) -> list[str]:
    options = [a.strip() for a in raw.split("|") if a.strip()]
    if not (2 <= len(options) <= 10):
        raise UserError("Gib 2 bis 10 Antworten an, durch `|` getrennt.")
    for option in options:
        if len(option) > MAX_ANSWER_LENGTH:
            raise UserError(f"„{option}“ ist zu lang (höchstens {MAX_ANSWER_LENGTH} Zeichen pro Antwort).")
    return options


async def setup(bot):
    await bot.add_cog(Polls(bot))
