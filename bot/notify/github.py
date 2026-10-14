"""GitHub-Provider: Releases, Issues, Pull Requests, Commits.

Per REST-API abgefragt (Polling), kein Webhook — ein Webhook bräuchte einen von GitHub aus
erreichbaren Port, Polling kommt mit einer ausgehenden Verbindung aus. Discussions sind bewusst
nicht dabei: die gibt es nur über die GraphQL-API, nicht über die hier genutzte REST-API.

Rate-Limit ohne `GITHUB_TOKEN`: 60 Anfragen/Stunde **insgesamt** (nicht pro Repo) — reicht nur für
wenige Feeds bei größerem Poll-Abstand. Mit einem Token (nur Lesen nötig): 5000/Stunde.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import aiohttp
import discord

from .base import Notification, Provider, ProviderError

log = logging.getLogger(__name__)
API = "https://api.github.com"

EVENTS = {
    "release": "🚀 Neue Releases",
    "issue_open": "🐛 Neue Issues",
    "issue_closed": "✅ Issues geschlossen",
    "pr_open": "🔀 Neue Pull Requests",
    "pr_closed": "🚫 Pull Requests geschlossen (nicht gemerged)",
    "pr_merged": "🟣 Pull Requests gemerged",
    "commit": "📝 Neue Commits",
}
COLOR = {
    "release": discord.Colour.gold(),
    "issue_open": discord.Colour.red(),
    "issue_closed": discord.Colour.green(),
    "pr_open": discord.Colour.blurple(),
    "pr_closed": discord.Colour.dark_grey(),
    "pr_merged": discord.Colour.purple(),
    "commit": discord.Colour.greyple(),
}


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "–"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return discord.utils.format_dt(dt, "f")


def _labels(item: dict) -> str:
    names = [l.get("name", "") for l in item.get("labels", []) if isinstance(l, dict)]
    return ", ".join(names) if names else "–"


def _match_filters(item: dict, filters: dict) -> bool:
    want_labels = filters.get("labels")
    if want_labels:
        have = {l.get("name", "").lower() for l in item.get("labels", []) if isinstance(l, dict)}
        if not (set(want_labels) & have):
            return False
    want_author = filters.get("author")
    if want_author:
        login = (item.get("user") or item.get("author") or {}).get("login", "")
        if login.lower() != want_author.lower():
            return False
    return True


class GitHubProvider(Provider):
    key = "github"
    label = "GitHub"
    events = EVENTS
    source_label = "Repository"
    source_example = "Navonex/thewashbot"
    source_help = "Format: Besitzer/Repo, z. B. Navonex/thewashbot"

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=_headers())
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, **params: Any) -> tuple[int, Any]:
        async with self._s().get(f"{API}{path}", params=params) as resp:
            if resp.status == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
                raise ProviderError(
                    "GitHub-Rate-Limit erreicht (ohne GITHUB_TOKEN nur 60 Anfragen/Stunde). "
                    "Entweder einen Token in die .env eintragen oder den Abstand vergrößern."
                )
            data = await resp.json() if resp.content_type == "application/json" else None
            return resp.status, data

    async def validate_source(self, source: str) -> str:
        source = source.strip().strip("/")
        if source.count("/") != 1 or not all(source.split("/")):
            raise ProviderError("Format muss „Besitzer/Repo“ sein, z. B. `Navonex/thewashbot`.")
        status, data = await self._get(f"/repos/{source}")
        if status == 404:
            raise ProviderError(f"Repository „{source}“ nicht gefunden (privat oder falsch geschrieben?).")
        if status != 200:
            raise ProviderError(f"GitHub antwortete mit Status {status}.")
        return data["full_name"]  # übernimmt die korrekte Groß-/Kleinschreibung von GitHub

    # ---- Einzelne Embeds ----------------------------------------------------------------

    def _release_embed(self, source: str, r: dict) -> discord.Embed:
        e = discord.Embed(
            title="🚀 Neuer Release", description=f"**{r.get('name') or r.get('tag_name')}**",
            url=r.get("html_url"), colour=COLOR["release"],
        )
        e.add_field(name="Repository", value=source)
        e.add_field(name="Autor", value=(r.get("author") or {}).get("login", "–"))
        e.add_field(name="Veröffentlicht", value=_fmt_date(r.get("published_at")))
        if r.get("prerelease"):
            e.set_footer(text="Pre-Release")
        return e

    def _issue_embed(self, source: str, i: dict, closed: bool) -> discord.Embed:
        e = discord.Embed(
            title="✅ Issue geschlossen" if closed else "🐛 Neues Issue",
            description=f"**#{i['number']} – {i['title']}**", url=i.get("html_url"),
            colour=COLOR["issue_closed" if closed else "issue_open"],
        )
        e.add_field(name="Repository", value=source)
        e.add_field(name="Autor", value=(i.get("user") or {}).get("login", "–"))
        e.add_field(name="Labels", value=_labels(i))
        return e

    def _pr_embed(self, source: str, p: dict, kind: str) -> discord.Embed:
        status_text = {"pr_open": "🟢 Open", "pr_closed": "🔴 Closed", "pr_merged": "🟣 Merged"}[kind]
        title = {"pr_open": "🔀 Pull Request", "pr_closed": "🚫 Pull Request geschlossen",
                "pr_merged": "🟣 Pull Request gemerged"}[kind]
        e = discord.Embed(title=title, description=f"**#{p['number']} – {p['title']}**",
                          url=p.get("html_url"), colour=COLOR[kind])
        e.add_field(name="Repository", value=source)
        e.add_field(name="Autor", value=(p.get("user") or {}).get("login", "–"))
        e.add_field(name="Status", value=status_text)
        return e

    def _commit_embed(self, source: str, c: dict) -> discord.Embed:
        commit = c.get("commit") or {}
        message = (commit.get("message") or "").split("\n", 1)[0][:200]
        author = (c.get("author") or {}).get("login") or (commit.get("author") or {}).get("name", "–")
        e = discord.Embed(title="📝 Neuer Commit", description=message or "(keine Beschreibung)",
                          url=c.get("html_url"), colour=COLOR["commit"])
        e.add_field(name="Repository", value=source)
        e.add_field(name="Autor", value=author)
        e.add_field(name="SHA", value=(c.get("sha") or "")[:7])
        return e

    # ---- Abfragen -------------------------------------------------------------------------

    async def _poll_releases(self, source: str, filters: dict, seen: list, out: list[Notification]) -> list:
        status, data = await self._get(f"/repos/{source}/releases", per_page=10)
        if status == 404:
            raise ProviderError(f"Repository „{source}“ scheint nicht mehr zu existieren.")
        if status != 200 or not isinstance(data, list):
            log.warning("GitHub releases für %s: Status %s", source, status)
            return seen
        seen = list(seen)
        for r in reversed(data):  # älteste zuerst, damit die Meldungsreihenfolge stimmt
            rid = r["id"]
            if rid in seen:
                continue
            seen.append(rid)
            if not filters.get("prereleases", True) and r.get("prerelease"):
                continue
            if not filters.get("drafts", True) and r.get("draft"):
                continue
            out.append(Notification("release", self._release_embed(source, r)))
        return seen[-50:]

    async def _poll_issues(
        self, source: str, filters: dict, seen: dict, events: list[str], out: list[Notification]
    ) -> dict:
        status, data = await self._get(
            f"/repos/{source}/issues", state="all", sort="updated", direction="desc", per_page=30
        )
        if status == 404:
            raise ProviderError(f"Repository „{source}“ scheint nicht mehr zu existieren.")
        if status != 200 or not isinstance(data, list):
            log.warning("GitHub issues für %s: Status %s", source, status)
            return seen
        seen = dict(seen)
        for i in reversed(data):
            if "pull_request" in i:  # die issues-API liefert PRs mit, die übernimmt _poll_prs
                continue
            if not _match_filters(i, filters):
                continue
            num = str(i["number"])
            was, now = seen.get(num), i["state"]
            seen[num] = now
            if was is None:
                if now == "open" and "issue_open" in events:
                    out.append(Notification("issue_open", self._issue_embed(source, i, False)))
            elif was == "open" and now == "closed" and "issue_closed" in events:
                out.append(Notification("issue_closed", self._issue_embed(source, i, True)))
        return seen

    async def _poll_prs(
        self, source: str, filters: dict, seen: dict, events: list[str], out: list[Notification]
    ) -> dict:
        status, data = await self._get(
            f"/repos/{source}/pulls", state="all", sort="updated", direction="desc", per_page=30
        )
        if status == 404:
            raise ProviderError(f"Repository „{source}“ scheint nicht mehr zu existieren.")
        if status != 200 or not isinstance(data, list):
            log.warning("GitHub pulls für %s: Status %s", source, status)
            return seen
        seen = dict(seen)
        for p in reversed(data):
            if not _match_filters(p, filters):
                continue
            num = str(p["number"])
            now = "merged" if p.get("merged_at") else p["state"]
            was = seen.get(num)
            seen[num] = now
            if was is None:
                if now == "open" and "pr_open" in events:
                    out.append(Notification("pr_open", self._pr_embed(source, p, "pr_open")))
            elif was != now:
                if now == "merged" and "pr_merged" in events:
                    out.append(Notification("pr_merged", self._pr_embed(source, p, "pr_merged")))
                elif now == "closed" and "pr_closed" in events:
                    out.append(Notification("pr_closed", self._pr_embed(source, p, "pr_closed")))
        return seen

    async def _poll_commits(self, source: str, seen: list, out: list[Notification]) -> list:
        status, data = await self._get(f"/repos/{source}/commits", per_page=10)
        if status == 404:
            raise ProviderError(f"Repository „{source}“ scheint nicht mehr zu existieren.")
        if status != 200 or not isinstance(data, list):
            log.warning("GitHub commits für %s: Status %s", source, status)
            return seen
        seen = list(seen)
        for c in reversed(data):
            sha = c["sha"]
            if sha in seen:
                continue
            seen.append(sha)
            out.append(Notification("commit", self._commit_embed(source, c)))
        return seen[-50:]

    async def poll(self, source, events, filters, state, *, baseline):
        out: list[Notification] = []
        new_state = dict(state)
        if "release" in events:
            new_state["releases"] = await self._poll_releases(source, filters, state.get("releases", []), out)
        if "issue_open" in events or "issue_closed" in events:
            new_state["issues"] = await self._poll_issues(source, filters, state.get("issues", {}), events, out)
        if any(k in events for k in ("pr_open", "pr_closed", "pr_merged")):
            new_state["prs"] = await self._poll_prs(source, filters, state.get("prs", {}), events, out)
        if "commit" in events:
            new_state["commits"] = await self._poll_commits(source, state.get("commits", []), out)
        if baseline:
            return [], new_state
        return out, new_state

    async def test_notification(self, source: str, events: list[str]) -> Notification:
        if "release" in events:
            status, data = await self._get(f"/repos/{source}/releases", per_page=1)
            if status == 200 and data:
                return Notification("release", self._release_embed(source, data[0]))
        if any(k in events for k in ("pr_open", "pr_closed", "pr_merged")):
            status, data = await self._get(f"/repos/{source}/pulls", state="all", per_page=1)
            if status == 200 and data:
                return Notification("pr_open", self._pr_embed(source, data[0], "pr_open"))
        if "issue_open" in events or "issue_closed" in events:
            status, data = await self._get(f"/repos/{source}/issues", state="all", per_page=5)
            if status == 200:
                for i in data or []:
                    if "pull_request" not in i:
                        return Notification("issue_open", self._issue_embed(source, i, False))
        if "commit" in events:
            status, data = await self._get(f"/repos/{source}/commits", per_page=1)
            if status == 200 and data:
                return Notification("commit", self._commit_embed(source, data[0]))
        e = discord.Embed(
            title="🧪 Test erfolgreich", colour=discord.Colour.green(),
            description=f"Verbindung zu **{source}** klappt, aber zu den gewählten Ereignissen "
                        "gibt es gerade noch nichts zum Anzeigen.",
        )
        return Notification("test", e)
