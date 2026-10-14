"""Gemeinsame Schnittstelle für Benachrichtigungs-Provider.

Ein Provider (siehe `github.py` für das erste Beispiel) bekommt Quelle, gewählte Ereignisse,
Filter und den zuletzt gespeicherten Zustand, und liefert neue Meldungen plus aktualisierten
Zustand zurück. Der Discord-Teil (`bot/cogs/notifications.py`) kennt nur diese Schnittstelle,
nichts GitHub-Spezifisches — ein neuer Dienst (YouTube, Twitch, RSS, …) muss nur `Provider`
implementieren und sich in `PROVIDERS` eintragen.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord


@dataclass
class Notification:
    """Eine einzelne Meldung, bereit fürs Posten."""

    event: str  # Schlüssel aus Provider.events, nur für Anzeige/Logging
    embed: discord.Embed


class ProviderError(Exception):
    """Nutzerverständlicher Fehlertext (ungültige Quelle, nicht erreichbar, Rate-Limit, …)."""


class Provider:
    key: str = ""
    label: str = ""
    events: dict[str, str] = {}  # Schlüssel -> Anzeigename, für Mehrfachauswahl + Zusammenfassung
    source_label: str = "Quelle"
    source_example: str = ""
    source_help: str = ""

    async def validate_source(self, source: str) -> str:
        """Wirft `ProviderError` bei ungültiger Eingabe, gibt sonst die normalisierte Quelle zurück."""
        raise NotImplementedError

    async def poll(
        self, source: str, events: list[str], filters: dict, state: dict, *, baseline: bool
    ) -> tuple[list[Notification], dict]:
        """Neue Meldungen seit `state`, plus aktualisierter Zustand zum Speichern.

        `baseline=True` beim allerersten Abruf eines Feeds: nur den Zustand füllen, nichts
        melden — sonst würde jeder schon bestehende Eintrag beim Einrichten als „neu" gemeldet.
        """
        raise NotImplementedError

    async def test_notification(self, source: str, events: list[str]) -> Notification:
        """Für den Test-Button: eine Beispiel-Meldung, ohne den gespeicherten Zustand zu ändern."""
        raise NotImplementedError

    async def close(self) -> None:
        pass
