"""Verwaltet die Benachrichtigungs-Provider (aktuell nur GitHub) und fragt fällige Feeds ab.

Discord-seitig (`bot/cogs/notifications.py`) kennt nur diese Klasse, nichts GitHub-Spezifisches —
ein neuer Dienst (YouTube, Twitch, RSS, …) kommt einfach in `PROVIDERS` dazu und muss nur
`bot.notify.base.Provider` implementieren.
"""

from __future__ import annotations

import json
import logging

import discord

from .base import Notification, Provider, ProviderError
from .github import GitHubProvider
from .youtube import YouTubeProvider

log = logging.getLogger(__name__)

PROVIDERS: dict[str, Provider] = {p.key: p for p in [GitHubProvider(), YouTubeProvider()]}


class NotificationManager:
    def __init__(self, db) -> None:
        self.db = db

    def provider(self, kind: str) -> Provider | None:
        return PROVIDERS.get(kind)

    async def close(self) -> None:
        for p in PROVIDERS.values():
            await p.close()

    async def poll_feed(self, feed, bot: discord.Client) -> list[Notification] | None:
        """Fragt einen Feed ab, postet neue Meldungen, speichert Zustand/Fehler.

        None, wenn nichts zu tun war (unbekannter Provider, deaktiviert, oder ein Fehler, der
        schon in `feeds.error` steht). Zustand wird nur aktualisiert, wenn das Posten (soweit
        möglich) auch tatsächlich geklappt hat — sonst gingen Meldungen bei einem vorübergehenden
        Problem (z. B. Kanal gerade nicht erreichbar) für immer verloren.
        """
        provider = self.provider(feed["kind"])
        if provider is None or not feed["enabled"]:
            return None

        events = json.loads(feed["events"] or "[]")
        filters = json.loads(feed["filters"] or "{}")
        state = json.loads(feed["state"] or "{}")
        baseline = not state
        try:
            notifications, new_state = await provider.poll(
                feed["source"], events, filters, state, baseline=baseline
            )
        except ProviderError as e:
            await self.db.execute("UPDATE feeds SET error=? WHERE id=?", (str(e), feed["id"]))
            log.warning("Feed %s (%s/%s): %s", feed["id"], feed["kind"], feed["source"], e)
            return None
        except Exception as e:
            log.exception("Feed %s (%s/%s): unerwarteter Fehler", feed["id"], feed["kind"], feed["source"])
            await self.db.execute(
                "UPDATE feeds SET error=? WHERE id=?", (f"{type(e).__name__}: {e}", feed["id"])
            )
            return None

        channel = bot.get_channel(feed["channel_id"])
        if notifications and channel is None:
            await self.db.execute(
                "UPDATE feeds SET error=? WHERE id=?",
                (f"Zielkanal nicht gefunden oder keine Berechtigung (Kanal-ID {feed['channel_id']}).", feed["id"]),
            )
            return None  # Zustand NICHT speichern, sonst gehen die Meldungen verloren

        posted: list[Notification] = []
        send_error = None
        for n in notifications:
            try:
                await channel.send(embed=n.embed)
                posted.append(n)
            except discord.HTTPException as e:
                send_error = f"Senden fehlgeschlagen: {e}"
                log.warning("Feed %s: %s", feed["id"], send_error)
                break  # Rest lieber beim nächsten Durchlauf erneut versuchen als halbe Sätze posten

        await self.db.execute(
            "UPDATE feeds SET state=?, error=? WHERE id=?", (json.dumps(new_state), send_error, feed["id"])
        )
        return posted
