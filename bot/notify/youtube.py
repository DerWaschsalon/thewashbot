"""YouTube-Provider: neue Videos per öffentlichem RSS-Feed, kein API-Schlüssel nötig.
Braucht die Kanal-ID (beginnt mit „UC…“) statt Handle/URL, weil die ID sich nie ändert und der
RSS-Feed sie direkt als Parameter erwartet — kein HTML-Scraping zum Auflösen eines Handles nötig.
"""

from __future__ import annotations

import logging
from xml.etree import ElementTree

import aiohttp
import discord

from .base import Notification, Provider, ProviderError

log = logging.getLogger(__name__)
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

EVENTS = {"video": "🎬 Neue Videos"}


def parse_feed(text: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        raise ProviderError("YouTube-Antwort konnte nicht gelesen werden.")
    videos = []
    for entry in root.findall("a:entry", NS):
        thumb = entry.find("media:group/media:thumbnail", NS)
        videos.append({
            "id": entry.findtext("yt:videoId", default="", namespaces=NS),
            "title": entry.findtext("a:title", default="", namespaces=NS),
            "author": entry.findtext("a:author/a:name", default="", namespaces=NS),
            "thumbnail": thumb.get("url") if thumb is not None else None,
        })
    return videos


class YouTubeProvider(Provider):
    key = "youtube"
    label = "YouTube"
    events = EVENTS
    source_label = "Kanal-ID"
    source_example = "UCX6OQ3DkcsbYNE6H8uQQuVA"
    source_help = (
        "Die Kanal-ID (beginnt mit „UC…“) — auf der Kanalseite über ⋮ → „Kanal-ID kopieren“, "
        "oder in der Seitenquelle nach „channelId“ suchen."
    )

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _fetch(self, channel_id: str) -> list[dict]:
        async with self._s().get(FEED_URL.format(channel_id=channel_id)) as resp:
            if resp.status == 404:
                raise ProviderError(f"Kein YouTube-Kanal mit der ID „{channel_id}“ gefunden.")
            if resp.status != 200:
                raise ProviderError(f"YouTube antwortete mit Status {resp.status}.")
            text = await resp.text()
        return parse_feed(text)

    async def validate_source(self, source: str) -> str:
        channel_id = source.strip()
        if not channel_id.startswith("UC"):
            raise ProviderError("Das sieht nicht nach einer YouTube-Kanal-ID aus (beginnt mit „UC…“).")
        await self._fetch(channel_id)  # wirft ProviderError, falls der Kanal nicht existiert
        return channel_id

    def _video_embed(self, channel_id: str, v: dict) -> discord.Embed:
        e = discord.Embed(
            title="🎬 Neues Video", description=f"**{v['title']}**",
            url=f"https://www.youtube.com/watch?v={v['id']}", colour=discord.Colour.red(),
        )
        e.add_field(name="Kanal", value=v.get("author") or channel_id)
        if v.get("thumbnail"):
            e.set_image(url=v["thumbnail"])
        return e

    async def poll(self, source, events, filters, state, *, baseline):
        videos = await self._fetch(source)
        seen = list(state.get("seen", []))
        out: list[Notification] = []
        for v in reversed(videos):  # älteste zuerst, damit die Meldungsreihenfolge stimmt
            if not v["id"] or v["id"] in seen:
                continue
            seen.append(v["id"])
            if "video" in events:
                out.append(Notification("video", self._video_embed(source, v)))
        new_state = {"seen": seen[-50:]}
        if baseline:
            return [], new_state
        return out, new_state

    async def test_notification(self, source: str, events: list[str]) -> Notification:
        videos = await self._fetch(source)
        if videos:
            return Notification("video", self._video_embed(source, videos[0]))
        e = discord.Embed(
            title="🧪 Test erfolgreich", colour=discord.Colour.green(),
            description=f"Verbindung zu **{source}** klappt, aber es gibt noch keine Videos.",
        )
        return Notification("test", e)
