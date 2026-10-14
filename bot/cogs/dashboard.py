"""Web-Dashboard: Einstellungen im Browser statt in Discord, mit Discord-Login.

Läuft als eigener aiohttp-Server im Bot (Port 8788, s. DASHBOARD_PORT). Login per Discord-OAuth2
(Scope nur "identify" — der Bot prüft die Rolle danach selbst über seinen eigenen Zugriff auf den
Server, es braucht also keine weiteren OAuth-Rechte vom Nutzer). Zugriff nur für Mitglieder mit der
per `/setup dashboard` festgelegten Rolle, ohne die: nur wer „Server verwalten“ hat.

Session: ein zufälliger Token in einem HttpOnly-Cookie, serverseitig in einem Dict gehalten (kein
Redis/DB nötig für eine Handvoll gleichzeitiger Logins, geht beim Neustart verloren — dann einfach
neu einloggen). Läuft über HTTP ohne eigenes TLS, daher nur fürs LAN/Tailscale gedacht, nicht fürs
offene Internet — siehe README.

Deckt die Einstellungen ab, die einfache Schlüssel/Wert-Paare sind. Der mehrstufige Assistent für
neue GitHub-Benachrichtigungen bleibt bewusst in Discord (siehe bot/cogs/notifications.py) — hier
gibt's nur Übersicht/An-Aus/Löschen dafür, Neuanlegen wäre als Formular kaum übersichtlicher als
der bestehende Klick-Assistent.
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
import time
import urllib.parse

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

log = logging.getLogger(__name__)

DEFAULT_PORT = 8788
SESSION_TTL = 24 * 3600
API = "https://discord.com/api/v10"
CLASS_EMOJI_NOTIFY = "🔔"

# session_id -> {"user_id": int, "name": str, "avatar": str|None, "expires": float}
SESSIONS: dict[str, dict] = {}


def dashboard_base_url() -> str:
    host = os.getenv("DASHBOARD_HOST") or "<LAN-IP-DIESES-SERVERS>"
    port = os.getenv("DASHBOARD_PORT", str(DEFAULT_PORT))
    return f"http://{host}:{port}"


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


NAV = [
    ("/", "Übersicht"), ("/general", "Allgemein"), ("/verification", "Verifizierung"),
    ("/tickets", "Tickets"), ("/dps", "DPS-Webhook"), ("/warnings", "Verwarnungen"),
    ("/notifications", "Benachrichtigungen"),
]


def layout(title: str, body: str, *, user: dict | None = None, active: str = "", flash: str = "") -> str:
    nav_html = "".join(
        f'<a href="{path}" class="{"active" if path == active else ""}">{label}</a>'
        for path, label in NAV
    )
    user_html = ""
    if user:
        user_html = f'<span class="user">{esc(user["name"])}</span> <a href="/logout">Abmelden</a>'
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} – TheWashBot</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; background: #f4f5f7; color: #1a1a1a; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #1a1b1e; color: #e4e4e7; }} }}
  header {{ background: #5865F2; color: #fff; padding: 14px 20px; display: flex; justify-content: space-between;
            align-items: center; flex-wrap: wrap; gap: 10px; }}
  header nav a {{ color: #fff; text-decoration: none; margin-right: 14px; padding: 4px 8px; border-radius: 6px; }}
  header nav a.active, header nav a:hover {{ background: rgba(255,255,255,.2); }}
  header .user {{ margin-right: 10px; opacity: .9; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 20px 16px 60px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 18px 20px; margin-bottom: 18px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  @media (prefers-color-scheme: dark) {{ .card {{ background: #2a2b2f; }} }}
  h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1.05rem; margin-top: 0; }}
  label {{ display: block; margin: 12px 0 4px; font-weight: 600; font-size: .9rem; }}
  select, input[type=text], textarea {{ width: 100%; box-sizing: border-box; padding: 8px 10px; border-radius: 6px;
            border: 1px solid #ccc; font-size: .95rem; background: transparent; color: inherit; }}
  textarea {{ min-height: 90px; font-family: inherit; }}
  .hint {{ font-size: .8rem; opacity: .7; margin-top: 2px; }}
  button {{ background: #5865F2; color: #fff; border: none; padding: 9px 18px; border-radius: 6px; font-size: .9rem;
            cursor: pointer; margin-top: 14px; }}
  button.danger {{ background: #ed4245; }}
  button.secondary {{ background: #6b7280; }}
  .flash {{ background: #d1fae5; color: #065f46; padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; }}
  @media (prefers-color-scheme: dark) {{ .flash {{ background: #064e3b; color: #a7f3d0; }} }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  td, th {{ text-align: left; padding: 6px 4px; border-bottom: 1px solid rgba(128,128,128,.25); }}
  .row-form {{ display: inline; }}
  code {{ background: rgba(128,128,128,.15); padding: 2px 5px; border-radius: 4px; }}
  .checklist label {{ font-weight: 400; display: flex; align-items: center; gap: 6px; margin: 4px 0; }}
</style></head><body>
<header><div><strong>🧺 TheWashBot</strong></div><nav>{nav_html}</nav><div>{user_html}</div></header>
<main>{flash_html}<h1>{esc(title)}</h1>{body}</main>
</body></html>"""


def channel_options(guild: discord.Guild, selected: int | None, *, types=(discord.ChannelType.text,)) -> str:
    opts = ['<option value="">– keiner –</option>']
    for ch in sorted((c for c in guild.channels if c.type in types), key=lambda c: c.position):
        sel = " selected" if selected and ch.id == selected else ""
        opts.append(f'<option value="{ch.id}"{sel}>#{esc(ch.name)}</option>')
    return "\n".join(opts)


def role_options(guild: discord.Guild, selected: int | None) -> str:
    opts = ['<option value="">– keine –</option>']
    for r in sorted((r for r in guild.roles if not r.is_default() and not r.managed), key=lambda r: -r.position):
        sel = " selected" if selected and r.id == selected else ""
        opts.append(f'<option value="{r.id}"{sel}>{esc(r.name)}</option>')
    return "\n".join(opts)


def role_checklist(guild: discord.Guild, name: str, selected_ids: list[int]) -> str:
    rows = []
    for r in sorted((r for r in guild.roles if not r.is_default() and not r.managed), key=lambda r: -r.position):
        checked = " checked" if r.id in selected_ids else ""
        rows.append(f'<label><input type="checkbox" name="{name}" value="{r.id}"{checked}> {esc(r.name)}</label>')
    return f'<div class="checklist">{"".join(rows)}</div>'


class Dashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.runner: web.AppRunner | None = None
        self._http: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        app = web.Application(middlewares=[self._error_middleware])
        app.add_routes([
            web.get("/login", self.login),
            web.get("/callback", self.callback),
            web.get("/logout", self.logout),
            web.get("/", self.page_overview),
            web.get("/general", self.page_general), web.post("/general", self.post_general),
            web.get("/verification", self.page_verification), web.post("/verification", self.post_verification),
            web.get("/tickets", self.page_tickets), web.post("/tickets", self.post_tickets),
            web.get("/dps", self.page_dps), web.post("/dps", self.post_dps),
            web.get("/warnings", self.page_warnings), web.post("/warnings", self.post_warnings),
            web.get("/notifications", self.page_notifications),
            web.post("/notifications/{id}/toggle", self.post_notification_toggle),
            web.post("/notifications/{id}/delete", self.post_notification_delete),
        ])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        port = int(os.getenv("DASHBOARD_PORT", DEFAULT_PORT))
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()
        log.info("Dashboard hört auf Port %d", port)

    async def cog_unload(self) -> None:
        if self.runner:
            await self.runner.cleanup()
        if self._http and not self._http.closed:
            await self._http.close()

    def http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    # ---- Hilfsfunktionen -------------------------------------------------------------------

    def guild(self) -> discord.Guild | None:
        gid = os.getenv("DEV_GUILD_ID", "").strip()
        return self.bot.get_guild(int(gid)) if gid.isdigit() else (self.bot.guilds[0] if self.bot.guilds else None)

    def oauth_redirect_uri(self) -> str:
        return f"{dashboard_base_url()}/callback"

    async def _session_user(self, request: web.Request) -> dict | None:
        sid = request.cookies.get("session")
        if not sid or sid not in SESSIONS:
            return None
        sess = SESSIONS[sid]
        if sess["expires"] < time.time():
            SESSIONS.pop(sid, None)
            return None
        return sess

    async def require_admin(self, request: web.Request) -> tuple[discord.Member, discord.Guild] | web.Response:
        guild = self.guild()
        if guild is None:
            return web.Response(text="Bot ist auf keinem Server.", status=500)
        sess = await self._session_user(request)
        if not sess:
            return web.HTTPFound("/login")
        member = guild.get_member(sess["user_id"])
        if member is None:
            try:
                member = await guild.fetch_member(sess["user_id"])
            except discord.NotFound:
                return web.HTTPFound("/login")
        admin_role_id = self.db.get_int(guild.id, "dashboard_admin_role")
        is_admin = member.guild_permissions.manage_guild
        if admin_role_id:
            role = guild.get_role(admin_role_id)
            is_admin = bool(role and role in member.roles)
        if not is_admin:
            return web.Response(
                text="Kein Zugriff — dir fehlt die für das Dashboard freigeschaltete Rolle "
                     "(oder „Server verwalten“). Frag einen Admin.",
                status=403,
            )
        return member, guild

    def _user_ctx(self, request: web.Request) -> dict | None:
        sess = SESSIONS.get(request.cookies.get("session") or "")
        return sess

    @web.middleware
    async def _error_middleware(self, request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception:
            log.exception("Dashboard-Fehler bei %s", request.path)
            return web.Response(text="Da ist etwas schiefgegangen. Details stehen im Log des Bots.", status=500)

    def _redirect(self, path: str, flash: str = "") -> web.Response:
        q = f"?ok={urllib.parse.quote(flash)}" if flash else ""
        return web.HTTPFound(f"{path}{q}")

    def _flash(self, request: web.Request) -> str:
        return request.query.get("ok", "")

    # ---- Login -------------------------------------------------------------------------------

    async def login(self, request: web.Request) -> web.Response:
        client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
        if not client_id:
            return web.Response(text="DISCORD_CLIENT_ID fehlt in der .env (Anwendungs-ID aus dem Developer Portal).", status=500)
        params = {
            "client_id": client_id, "redirect_uri": self.oauth_redirect_uri(),
            "response_type": "code", "scope": "identify",
        }
        return web.HTTPFound(f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(params)}")

    async def callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code")
        if not code:
            return web.Response(text="Anmeldung abgebrochen.", status=400)
        client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
        client_secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            return web.Response(text="DISCORD_CLIENT_ID/DISCORD_CLIENT_SECRET fehlen in der .env.", status=500)
        data = {
            "client_id": client_id, "client_secret": client_secret, "grant_type": "authorization_code",
            "code": code, "redirect_uri": self.oauth_redirect_uri(),
        }
        async with self.http().post(f"{API}/oauth2/token", data=data) as resp:
            if resp.status != 200:
                return web.Response(text=f"Anmeldung bei Discord fehlgeschlagen ({resp.status}).", status=502)
            token_data = await resp.json()
        async with self.http().get(
            f"{API}/users/@me", headers={"Authorization": f"Bearer {token_data['access_token']}"}
        ) as resp:
            if resp.status != 200:
                return web.Response(text="Konnte Discord-Konto nicht lesen.", status=502)
            user = await resp.json()

        sid = secrets.token_urlsafe(32)
        SESSIONS[sid] = {
            "user_id": int(user["id"]), "name": user.get("global_name") or user["username"],
            "avatar": user.get("avatar"), "expires": time.time() + SESSION_TTL,
        }
        resp = web.HTTPFound("/")
        resp.set_cookie("session", sid, httponly=True, samesite="Lax", max_age=SESSION_TTL)
        return resp

    async def logout(self, request: web.Request) -> web.Response:
        sid = request.cookies.get("session")
        if sid:
            SESSIONS.pop(sid, None)
        resp = web.HTTPFound("/login")
        resp.del_cookie("session")
        return resp

    # ---- Übersicht ---------------------------------------------------------------------------

    async def page_overview(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        db = self.db
        rows = [
            ("Log-Kanal", db.get_int(guild.id, "log_channel")),
            ("Willkommenskanal", db.get_int(guild.id, "welcome_channel")),
            ("Autorolle", db.get_int(guild.id, "autorole")),
            ("Temp-Voice", db.get_int(guild.id, "tempvoice_hub")),
            ("DJ-Rolle", db.get_int(guild.id, "dj_role")),
        ]

        def fmt(v):
            if not v:
                return "–"
            ch = guild.get_channel(v)
            if ch:
                return f"#{esc(ch.name)}"
            role = guild.get_role(v)
            return esc(role.name) if role else f"({v})"

        body = '<div class="card"><table>' + "".join(
            f"<tr><td>{esc(label)}</td><td>{fmt(value)}</td></tr>" for label, value in rows
        ) + "</table></div>"
        body += (
            '<div class="card"><h2>Hallo 👋</h2><p>Links oben die Bereiche. Neue '
            "GitHub-Benachrichtigungen legst du weiterhin direkt in Discord an "
            '(🔔-Button im Einstellungen-Kanal) — hier kannst du bestehende an/ausschalten oder '
            "löschen.</p></div>"
        )
        return web.Response(
            text=layout("Übersicht", body, user=self._user_ctx(request), active="/", flash=self._flash(request)),
            content_type="text/html",
        )

    # ---- Allgemein ---------------------------------------------------------------------------

    async def page_general(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        db = self.db
        body = f"""
<form method="post" class="card">
  <h2>Moderation</h2>
  <label>Log-Kanal</label>
  <select name="log_channel">{channel_options(guild, db.get_int(guild.id, "log_channel"))}</select>
  <div class="hint">Gelöschte/bearbeitete Nachrichten, Beitritte, Bans, …</div>
  <label><input type="checkbox" name="antispam" {"checked" if db.get_bool(guild.id, "antispam", True) else ""}> Spamschutz aktiv</label>
  <label><input type="checkbox" name="allow_invites" {"checked" if db.get_bool(guild.id, "allow_invites") else ""}> Einladungslinks erlauben</label>
  <button type="submit">Speichern</button>
</form>
<form method="post" class="card">
  <h2>Willkommen</h2>
  <label>Willkommenskanal</label>
  <select name="welcome_channel">{channel_options(guild, db.get_int(guild.id, "welcome_channel"))}</select>
  <label>Begrüßungstext</label>
  <textarea name="welcome_text">{esc(db.get(guild.id, "welcome_text", ""))}</textarea>
  <div class="hint">Platzhalter: <code>{{user}}</code> <code>{{name}}</code> <code>{{server}}</code> <code>{{anzahl}}</code></div>
  <label>Autorolle (z. B. „Neuling“, sofort bei Beitritt)</label>
  <select name="autorole">{role_options(guild, db.get_int(guild.id, "autorole"))}</select>
  <button type="submit">Speichern</button>
</form>
<form method="post" class="card">
  <h2>Musik &amp; Voice</h2>
  <label>DJ-Rolle (leer = alle dürfen steuern)</label>
  <select name="dj_role">{role_options(guild, db.get_int(guild.id, "dj_role"))}</select>
  <label>Temp-Voice-Kanal</label>
  <select name="tempvoice_hub">{channel_options(guild, db.get_int(guild.id, "tempvoice_hub"), types=(discord.ChannelType.voice,))}</select>
  <div class="hint">Neu anlegen/ändern geht genauer per <code>/setup temp-voice</code> in Discord.</div>
  <button type="submit">Speichern</button>
</form>"""
        return web.Response(
            text=layout("Allgemein", body, user=self._user_ctx(request), active="/general", flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_general(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        await self.db.set(guild.id, "log_channel", form.get("log_channel") or None)
        await self.db.set(guild.id, "antispam", "antispam" in form)
        await self.db.set(guild.id, "allow_invites", "allow_invites" in form)
        await self.db.set(guild.id, "welcome_channel", form.get("welcome_channel") or None)
        await self.db.set(guild.id, "welcome_text", str(form.get("welcome_text") or "") or None)
        await self.db.set(guild.id, "autorole", form.get("autorole") or None)
        await self.db.set(guild.id, "dj_role", form.get("dj_role") or None)
        await self.db.set(guild.id, "tempvoice_hub", form.get("tempvoice_hub") or None)
        return self._redirect("/general", "Gespeichert.")

    # ---- Verifizierung -----------------------------------------------------------------------

    async def page_verification(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        db = self.db
        channel_id = db.get_int(guild.id, "verify_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        verify_role = guild.get_role(db.get_int(guild.id, "verify_role") or 0)
        message_id = db.get_int(guild.id, "verify_message")
        body = f"""
<div class="card">
  <h2>Status</h2>
  <p>Regel-Kanal: {("#" + esc(channel.name)) if channel else "noch nicht eingerichtet"}<br>
     Verifiziert-Rolle: {esc(verify_role.name) if verify_role else "–"}</p>
  <div class="hint">Komplett neu einrichten (Kanal/Rolle/Rechte) geht nur per
     <code>/setup verifizierung</code> in Discord, damit die Vorschau+Bestätigung nicht übersprungen wird.</div>
</div>
<form method="post" class="card">
  <h2>Regel-Text ändern</h2>
  <label>Text im Regeln-Panel</label>
  <textarea name="text" placeholder="Nur ausfüllen, wenn du den Text ändern willst"></textarea>
  <div class="hint">{"Panel-Nachricht gefunden, kann bearbeitet werden." if message_id else "Noch kein Panel gepostet — erst /setup verifizierung in Discord ausführen."}</div>
  <button type="submit" {"disabled" if not message_id else ""}>Text speichern</button>
</form>"""
        return web.Response(
            text=layout("Verifizierung", body, user=self._user_ctx(request), active="/verification", flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_verification(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        text = str(form.get("text") or "").strip()
        if not text:
            return self._redirect("/verification", "Kein Text eingegeben, nichts geändert.")
        channel_id = self.db.get_int(guild.id, "verify_channel")
        message_id = self.db.get_int(guild.id, "verify_message")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not channel or not message_id:
            return self._redirect("/verification", "Noch kein Panel eingerichtet.")
        try:
            message = await channel.fetch_message(message_id)
            embed = message.embeds[0] if message.embeds else discord.Embed(title="📜 Regeln")
            embed.description = text
            await message.edit(embed=embed)
        except discord.HTTPException as e:
            return self._redirect("/verification", f"Fehler: {e}")
        return self._redirect("/verification", "Regel-Text aktualisiert.")

    # ---- Tickets -------------------------------------------------------------------------------

    async def page_tickets(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        selected = self.db.get_ids(guild.id, "ticket_team_roles")
        body = f"""
<form method="post" class="card">
  <h2>Team-Rollen (sehen neue Tickets, dürfen schließen)</h2>
  {role_checklist(guild, "team_roles", selected)}
  <button type="submit">Speichern</button>
</form>
<div class="card hint">Panel in einem Kanal posten: <code>/ticket-panel</code> in Discord ausführen.</div>"""
        return web.Response(
            text=layout("Tickets", body, user=self._user_ctx(request), active="/tickets", flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_tickets(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        ids = form.getall("team_roles", [])
        await self.db.set(guild.id, "ticket_team_roles", ",".join(ids) or None)
        return self._redirect("/tickets", "Gespeichert.")

    # ---- DPS-Webhook ---------------------------------------------------------------------------

    async def page_dps(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        from .dps import webhook_url  # lokal gegen Import-Zyklus

        token = self.db.get(guild.id, "dps_webhook_token")
        url_html = f"<code>{esc(webhook_url(token))}</code>" if token else "noch nicht eingerichtet"
        body = f"""
<form method="post" class="card">
  <h2>Ziel-Kanal</h2>
  <select name="channel">{channel_options(guild, self.db.get_int(guild.id, "dps_webhook_channel"))}</select>
  <div class="hint">Webhook-URL für DeckBar: {url_html}</div>
  <button type="submit">Speichern</button>
</form>"""
        return web.Response(
            text=layout("DPS-Webhook", body, user=self._user_ctx(request), active="/dps", flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_dps(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        from .dps import ensure_webhook

        form = await request.post()
        channel_id = form.get("channel")
        if not channel_id:
            return self._redirect("/dps", "Kein Kanal gewählt, nichts geändert.")
        await ensure_webhook(self.db, guild.id, int(channel_id))
        return self._redirect("/dps", "Gespeichert.")

    # ---- Verwarnungen --------------------------------------------------------------------------

    async def page_warnings(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        db = self.db
        threshold = db.get_int(guild.id, "warn_threshold") or 0
        action = db.get(guild.id, "warn_action") or "timeout"
        opts = "".join(
            f'<option value="{a}"{" selected" if a == action else ""}>{a}</option>' for a in ("timeout", "kick", "ban")
        )
        body = f"""
<form method="post" class="card">
  <h2>Automatische Strafe</h2>
  <label>Schwelle (0 = aus)</label>
  <input type="text" name="threshold" value="{threshold}">
  <label>Aktion</label>
  <select name="action">{opts}</select>
  <button type="submit">Speichern</button>
</form>
<div class="card hint">Einzelne Verwarnungen (aussprechen, Liste, löschen) laufen über
  <code>/warn</code>, <code>/warnungen</code>, <code>/warnung-loeschen</code> in Discord.</div>"""
        return web.Response(
            text=layout("Verwarnungen", body, user=self._user_ctx(request), active="/warnings", flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_warnings(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        try:
            threshold = max(0, int(form.get("threshold") or 0))
        except ValueError:
            threshold = 0
        if threshold == 0:
            await self.db.set(guild.id, "warn_threshold", None)
            await self.db.set(guild.id, "warn_action", None)
        else:
            await self.db.set(guild.id, "warn_threshold", threshold)
            await self.db.set(guild.id, "warn_action", str(form.get("action") or "timeout"))
        return self._redirect("/warnings", "Gespeichert.")

    # ---- Benachrichtigungen --------------------------------------------------------------------

    async def page_notifications(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        from ..notify.manager import PROVIDERS

        feeds = await self.db.fetchall("SELECT * FROM feeds WHERE guild_id=?", (guild.id,))
        rows = []
        for f in feeds:
            provider = PROVIDERS.get(f["kind"])
            events = json.loads(f["events"] or "[]")
            names = ", ".join((provider.events.get(e, e) if provider else e) for e in events)
            channel = guild.get_channel(f["channel_id"])
            rows.append(f"""<tr>
  <td>{esc(provider.label if provider else f["kind"])}: {esc(f["source"])}</td>
  <td>{esc(names)}</td>
  <td>{("#" + esc(channel.name)) if channel else "–"}</td>
  <td>{"🟢" if f["enabled"] else "⏸️"}</td>
  <td>
    <form class="row-form" method="post" action="/notifications/{f['id']}/toggle">
      <button type="submit" class="secondary">{"Deaktivieren" if f["enabled"] else "Aktivieren"}</button>
    </form>
    <form class="row-form" method="post" action="/notifications/{f['id']}/delete"
          onsubmit="return confirm('{esc(f['source'])} wirklich löschen?')">
      <button type="submit" class="danger">Löschen</button>
    </form>
  </td>
</tr>""")
        table = (
            "<table><tr><th>Quelle</th><th>Ereignisse</th><th>Kanal</th><th>Status</th><th></th></tr>"
            + "".join(rows) + "</table>" if rows else "<p>Noch keine Benachrichtigung eingerichtet.</p>"
        )
        body = f'<div class="card">{table}</div><div class="card hint">Neu anlegen: Discord → Einstellungen-Kanal → „🔔 Benachrichtigungen“ → „➕ Neue Benachrichtigung“.</div>'
        return web.Response(
            text=layout("Benachrichtigungen", body, user=self._user_ctx(request), active="/notifications", flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_notification_toggle(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        feed_id = int(request.match_info["id"])
        row = await self.db.fetchone("SELECT enabled FROM feeds WHERE id=?", (feed_id,))
        if row:
            await self.db.execute("UPDATE feeds SET enabled=? WHERE id=?", (0 if row["enabled"] else 1, feed_id))
        return self._redirect("/notifications", "Gespeichert.")

    async def post_notification_delete(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        feed_id = int(request.match_info["id"])
        await self.db.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
        return self._redirect("/notifications", "Gelöscht.")


async def setup(bot):
    await bot.add_cog(Dashboard(bot))
