"""Musikquellen: YouTube/SoundCloud über yt-dlp, Spotify-Links, Internetradio (radio-browser.info).

Ein `Track` merkt sich nur, *was* gespielt werden soll. Die eigentliche Stream-Adresse holt
`stream_url()` erst kurz vor dem Abspielen, weil YouTube-Adressen nach einigen Stunden ablaufen.
Spotify liefert selbst kein Audio: Titel und Interpret werden gelesen und auf YouTube gesucht.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import socket
import time
from dataclasses import dataclass, field
from html import unescape

import aiohttp
import yt_dlp

from .errors import UserError

log = logging.getLogger(__name__)

MAX_PLAYLIST = int(os.getenv("MAX_PLAYLIST", "100"))
USER_AGENT = "DiscordMusikBot/1.0 (+https://github.com/)"


@dataclass
class Track:
    title: str
    query: str                     # URL oder "ytsearch1:…", aufgelöst erst beim Abspielen
    requester_id: int
    duration: int | None = None    # Sekunden
    page_url: str | None = None
    thumbnail: str | None = None
    live: bool = False
    direct: bool = False           # query ist bereits ein Audiostream (Radio)
    headers: dict = field(default_factory=dict)


# ---- yt-dlp ------------------------------------------------------------------------------

def _ytdl_base() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "source_address": "0.0.0.0",
        "default_search": "ytsearch",
        "socket_timeout": 15,
    }
    cookies = os.getenv("YTDLP_COOKIES", "").strip()
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    return opts


def _extract(query: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(query, download=False)


def _best_thumbnail(info: dict) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    return thumbs[-1].get("url") if thumbs else None


def _track_from_info(info: dict, requester_id: int) -> Track:
    url = info.get("webpage_url") or info.get("url") or info.get("original_url")
    duration = info.get("duration")
    return Track(
        title=info.get("title") or url or "Unbekannt",
        query=url,
        requester_id=requester_id,
        duration=int(duration) if duration else None,
        page_url=url if url and url.startswith("http") else None,
        thumbnail=_best_thumbnail(info),
        live=bool(info.get("is_live") or info.get("live_status") == "is_live"),
    )


def is_url(text: str) -> bool:
    return bool(re.match(r"https?://", text.strip()))


async def resolve(query: str, requester_id: int, session: aiohttp.ClientSession) -> tuple[list[Track], str | None]:
    """Link oder Suchbegriff → Tracks und ggf. Name der Playlist."""
    query = query.strip()
    m = SPOTIFY_RE.search(query)
    if m:
        return await spotify_tracks(session, m.group(1), m.group(2), requester_id)

    search = not is_url(query)
    target = f"ytsearch1:{query}" if search else query
    opts = {**_ytdl_base(), "extract_flat": "in_playlist", "playlistend": MAX_PLAYLIST}
    try:
        info = await asyncio.to_thread(_extract, target, opts)
    except yt_dlp.utils.DownloadError as e:
        raise UserError(f"Konnte das nicht laden: {_short_error(e)}") from None
    if not info:
        raise UserError("Nichts gefunden.")

    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise UserError("Nichts gefunden.")
        tracks = [_track_from_info(e, requester_id) for e in entries[:MAX_PLAYLIST]]
        return tracks, (None if search else info.get("title"))
    return [_track_from_info(info, requester_id)], None


async def stream_url(track: Track) -> str:
    """Frische Stream-Adresse holen, dabei Titel/Länge/Bild des Tracks vervollständigen."""
    if track.direct:
        return track.query
    opts = {**_ytdl_base(), "format": "bestaudio/best", "noplaylist": True}
    info = await asyncio.to_thread(_extract, track.query, opts)
    if info and "entries" in info:
        info = next((e for e in info["entries"] if e), None)
    if not info or not info.get("url"):
        raise UserError("keine Audiospur gefunden")
    if not track.title or track.title == track.query:
        track.title = info.get("title") or track.title
    track.duration = track.duration or (int(info["duration"]) if info.get("duration") else None)
    track.page_url = track.page_url or info.get("webpage_url")
    track.thumbnail = track.thumbnail or _best_thumbnail(info)
    track.live = track.live or bool(info.get("is_live"))
    track.headers = info.get("http_headers") or {}
    return info["url"]


def _short_error(e: Exception) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    text = text.replace("ERROR: ", "")
    return text[:200]


# ---- Spotify -----------------------------------------------------------------------------

SPOTIFY_RE = re.compile(r"https?://open\.spotify\.com/(?:intl-[\w-]+/)?(track|album|playlist)/([A-Za-z0-9]+)")
_spotify_token: tuple[str, float] | None = None


def _spotify_track(name: str, artists: str, duration_ms: int | None, requester_id: int) -> Track:
    title = f"{artists} – {name}" if artists else name
    return Track(
        title=title,
        query=f"ytsearch1:{artists} {name} audio",
        requester_id=requester_id,
        duration=int(duration_ms / 1000) if duration_ms else None,
    )


async def _spotify_api_token(session: aiohttp.ClientSession) -> str | None:
    global _spotify_token
    cid = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        return None
    if _spotify_token and _spotify_token[1] > time.time() + 60:
        return _spotify_token[0]
    async with session.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=aiohttp.BasicAuth(cid, secret),
    ) as r:
        if r.status != 200:
            log.warning("Spotify-Token: HTTP %s", r.status)
            return None
        data = await r.json()
    _spotify_token = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    return _spotify_token[0]


async def _spotify_via_api(session, token, kind, sid, requester_id) -> tuple[list[Track], str | None]:
    headers = {"Authorization": f"Bearer {token}"}
    base = "https://api.spotify.com/v1"

    async def get(url):
        async with session.get(url, headers=headers) as r:
            if r.status == 404:
                raise UserError("Spotify: nicht gefunden oder nicht öffentlich.")
            r.raise_for_status()
            return await r.json()

    def artists(t):
        return ", ".join(a["name"] for a in t.get("artists") or [])

    if kind == "track":
        t = await get(f"{base}/tracks/{sid}")
        return [_spotify_track(t["name"], artists(t), t.get("duration_ms"), requester_id)], None

    if kind == "album":
        album = await get(f"{base}/albums/{sid}")
        items, page = list(album["tracks"]["items"]), album["tracks"].get("next")
    else:
        pl = await get(f"{base}/playlists/{sid}?fields=name")
        album = {"name": pl.get("name")}
        data = await get(f"{base}/playlists/{sid}/tracks?limit=100")
        items = [i.get("track") for i in data["items"]]
        page = data.get("next")
    while page and len(items) < MAX_PLAYLIST:
        data = await get(page)
        items += [i.get("track") if kind == "playlist" else i for i in data["items"]]
        page = data.get("next")
    tracks = [
        _spotify_track(t["name"], artists(t), t.get("duration_ms"), requester_id)
        for t in items[:MAX_PLAYLIST] if t and t.get("name")
    ]
    return tracks, album.get("name")


def _find_key(obj, key):
    """Erstes Vorkommen eines Schlüssels in verschachteltem JSON."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


async def _spotify_via_embed(session, kind, sid, requester_id) -> tuple[list[Track], str | None]:
    """Ohne API-Schlüssel: die öffentliche Einbettungsseite lesen (bis ~100 Titel)."""
    url = f"https://open.spotify.com/embed/{kind}/{sid}"
    async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
        if r.status != 200:
            raise UserError("Spotify-Link konnte nicht gelesen werden.")
        html = await r.text()
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', html, re.S)
    if not m:
        raise UserError("Spotify hat das Seitenformat geändert. Mit SPOTIFY_CLIENT_ID/SECRET geht es über die API.")
    data = json.loads(m.group(1))
    entity = _find_key(data, "entity") or {}
    track_list = entity.get("trackList") or _find_key(data, "trackList") or []
    if kind == "track" or not track_list:
        name = entity.get("name") or entity.get("title")
        artists = ", ".join(a.get("name", "") for a in entity.get("artists") or [])
        if not name:
            raise UserError("Spotify-Titel nicht gefunden.")
        return [_spotify_track(name, artists, entity.get("duration"), requester_id)], None
    tracks = [
        _spotify_track(t.get("title", ""), unescape(t.get("subtitle", "")).replace(" ", " "),
                       t.get("duration"), requester_id)
        for t in track_list[:MAX_PLAYLIST] if t.get("title")
    ]
    return tracks, entity.get("name") or entity.get("title")


async def spotify_tracks(session, kind, sid, requester_id) -> tuple[list[Track], str | None]:
    try:
        token = await _spotify_api_token(session)
        if token:
            return await _spotify_via_api(session, token, kind, sid, requester_id)
        return await _spotify_via_embed(session, kind, sid, requester_id)
    except UserError:
        raise
    except Exception as e:
        log.warning("Spotify %s/%s: %r", kind, sid, e)
        raise UserError("Spotify-Link konnte nicht gelesen werden.") from None


# ---- Internetradio -----------------------------------------------------------------------

class RadioBrowser:
    """Senderverzeichnis radio-browser.info. Server werden per DNS gesucht, wie die API es verlangt."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._hosts: list[str] = []

    def _lookup_hosts(self) -> list[str]:
        hosts = set()
        try:
            for *_, addr in socket.getaddrinfo("all.api.radio-browser.info", 443, proto=socket.IPPROTO_TCP):
                try:
                    hosts.add(socket.gethostbyaddr(addr[0])[0])
                except OSError:
                    pass
        except OSError:
            pass
        hosts = [h for h in hosts if h.endswith("radio-browser.info")] or ["de1.api.radio-browser.info"]
        random.shuffle(hosts)
        return hosts

    async def _get(self, path: str, params: dict | None = None):
        if not self._hosts:
            self._hosts = await asyncio.to_thread(self._lookup_hosts)
        last = None
        for host in list(self._hosts):
            try:
                async with self.session.get(
                    f"https://{host}{path}", params=params,
                    headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=4),
                ) as r:
                    r.raise_for_status()
                    return await r.json()
            except Exception as e:
                last = e
                self._hosts.remove(host)
                self._hosts.append(host)
        raise UserError(f"Radioverzeichnis nicht erreichbar ({last!r})")

    async def search(self, name: str, limit: int = 15) -> list[dict]:
        return await self._get("/json/stations/search", {
            "name": name, "limit": limit, "order": "votes", "reverse": "true", "hidebroken": "true",
        })

    async def station(self, uuid: str) -> dict | None:
        result = await self._get(f"/json/stations/byuuid/{uuid}")
        return result[0] if result else None

    async def click(self, uuid: str) -> None:
        """Zählt den Aufruf beim Verzeichnis, so wie die API es sich wünscht."""
        try:
            await self._get(f"/json/url/{uuid}")
        except Exception:
            pass

    @staticmethod
    def to_track(st: dict, requester_id: int) -> Track:
        return Track(
            title=st.get("name", "Radio").strip(),
            query=st.get("url_resolved") or st.get("url"),
            requester_id=requester_id,
            page_url=st.get("homepage") or None,
            thumbnail=st.get("favicon") or None,
            live=True,
            direct=True,
        )
