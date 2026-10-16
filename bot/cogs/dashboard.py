"""Web-Dashboard: Einstellungen im Browser statt in Discord, mit Discord-Login.

Läuft als eigener aiohttp-Server im Bot (Port 8789, s. DASHBOARD_PORT). Login per Discord-OAuth2
(Scope nur "identify" — der Bot prüft die Rolle danach selbst über seinen eigenen Zugriff auf den
Server, es braucht also keine weiteren OAuth-Rechte vom Nutzer). Zugriff nur für Mitglieder mit der
per `/setup dashboard` festgelegten Rolle, ohne die: nur wer „Server verwalten“ hat.

Session: ein zufälliger Token in einem HttpOnly-Cookie, serverseitig in einem Dict gehalten (kein
Redis/DB nötig für eine Handvoll gleichzeitiger Logins, geht beim Neustart verloren — dann einfach
neu einloggen). Läuft über HTTP ohne eigenes TLS, daher nur fürs LAN/Tailscale gedacht, nicht fürs
offene Internet — siehe README.

Deckt inzwischen praktisch alle Einstellungen ab, die als Formular Sinn ergeben — Allgemein,
Automod, Verwarnungen, Tickets, Community (Zähler/Starboard/Geburtstage/Bump-Erinnerung),
Levelsystem, zeitgesteuerte Ankündigungen, eigene Befehle, DPS-Webhook, AION-2-Filter,
Benachrichtigungen, Sichtbarkeits-Matrix. Bewusst NICHT hier, weiterhin nur in Discord:
- der mehrstufige Assistent für neue GitHub/YouTube-Benachrichtigungen (hier nur
  Übersicht/An-Aus/Löschen) und die Verifizierungs-Ersteinrichtung — beide wegen
  Vorschau/Bestätigung bzw. serverweiter Rechte-Änderungen, die man nicht nebenbei in einem
  Formular anklicken sollte;
- Giveaways — laufende Zeitgeber/Teilnehmer sind kein Schlüssel/Wert-Setting;
- Server-Status (`/server`) — braucht den Docker-Socket, bewusst nur für `OWNER_ID` in Discord,
  nicht über die rollenbasierte Dashboard-Berechtigung.

Optik angelehnt an DeckBar (github.com/gamunex/deckbar, eigenes Projekt desselben Entwicklers):
dunkler Verlaufshintergrund, transluzente „Liquid-Glass“-Karten, blauer Akzent, Pill-Chips für
Status — siehe `DASHBOARD_CSS` unten.
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

DEFAULT_PORT = 8789
SESSION_TTL = 24 * 3600
API = "https://discord.com/api/v10"
CLASS_EMOJI_NOTIFY = "🔔"

# session_id -> {"user_id": int, "name": str, "avatar": str|None, "expires": float}
SESSIONS: dict[str, dict] = {}


def dashboard_base_url() -> str:
    """Basis-URL für den OAuth2-Redirect. `DASHBOARD_PUBLIC_URL` (volle URL, z. B. ein
    Cloudflare-Tunnel-Hostname über https, ohne Port) hat Vorrang — sonst `http://<DASHBOARD_HOST>:
    <DASHBOARD_PORT>` fürs reine LAN/Tailscale. Muss exakt zur im Developer Portal unter
    OAuth2 → Redirects eingetragenen URL passen."""
    public_url = os.getenv("DASHBOARD_PUBLIC_URL", "").strip()
    if public_url:
        return public_url.rstrip("/")
    host = os.getenv("DASHBOARD_HOST") or "<LAN-IP-DIESES-SERVERS>"
    port = os.getenv("DASHBOARD_PORT", str(DEFAULT_PORT))
    return f"http://{host}:{port}"


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


NAV = [
    ("/", "Übersicht"), ("/general", "Allgemein"), ("/verification", "Verifizierung"),
    ("/automod", "Automod"), ("/warnings", "Verwarnungen"), ("/tickets", "Tickets"),
    ("/community", "Community"), ("/level", "Levelsystem"), ("/announcements", "Ankündigungen"),
    ("/commands", "Eigene Befehle"), ("/dps", "DPS-Webhook"), ("/aion", "AION-2-Filter"),
    ("/notifications", "Benachrichtigungen"), ("/matrix", "Sichtbarkeit"),
]

# Liquid-Glass-Optik angelehnt an DeckBar (github.com/gamunex/deckbar, daemon/deckbar_daemon/web/style.css):
# dunkler Verlaufshintergrund, transluzente Karten mit dezentem Rand, blauer Akzent, Pill-Chips.
DASHBOARD_CSS = """
:root {
  --bg: #0b0e1a;
  --panel: rgba(255, 255, 255, 0.055);
  --panel-2: rgba(255, 255, 255, 0.09);
  --line: rgba(255, 255, 255, 0.12);
  --text: #f2f3f7;
  --dim: rgba(242, 243, 247, 0.62);
  --accent: #5ac8fa;
  --danger: #ff453a;
  --success: #30d158;
  --warning: #ff9f0a;
  --radius: 14px;
  color-scheme: dark;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background:
    radial-gradient(900px 400px at 10% -10%, rgba(88, 60, 220, 0.28), transparent 70%),
    radial-gradient(800px 400px at 100% 0%, rgba(230, 110, 60, 0.18), transparent 70%),
    var(--bg);
  color: var(--text);
  font: 14px/1.5 Inter, system-ui, -apple-system, "Segoe UI", sans-serif;
  min-height: 100vh;
  overflow-x: hidden; /* Sicherheitsnetz gegen seitliches Scrollen auf schmalen Bildschirmen */
}
h1, h2 { margin: 0; font-weight: 600; }
h1 { font-size: 20px; letter-spacing: 0.2px; margin-bottom: 18px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--dim); margin-bottom: 14px; }

label { display: block; margin: 14px 0 5px; font-size: 13px; font-weight: 500; color: var(--dim); }
select, input[type=text], input[type=number], textarea {
  width: 100%; background: rgba(0, 0, 0, 0.28); border: 1px solid var(--line);
  border-radius: 9px; padding: 10px 12px; font: inherit; font-size: 16px; color: inherit; outline: none;
  /* 16px verhindert, dass iOS Safari beim Fokussieren automatisch hineinzoomt */
}
select:focus, input:focus, textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(90, 200, 250, 0.18); }
textarea { min-height: 90px; font-family: inherit; resize: vertical; }
input[type=checkbox] { width: auto; height: 18px; margin-right: 6px; accent-color: var(--accent); flex-shrink: 0; }

.hint { font-size: 12px; color: var(--dim); margin-top: 6px; }
.hint code { white-space: normal; }

button {
  background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 9px;
  padding: 10px 18px; min-height: 40px; font: inherit; font-size: 14px; font-weight: 500; cursor: pointer;
  margin-top: 16px; touch-action: manipulation;
}
button:hover { background: rgba(255, 255, 255, 0.14); }
button[type=submit] { background: var(--accent); border-color: transparent; color: #04121a; font-weight: 600; }
button[type=submit]:hover { filter: brightness(1.08); }
button.danger { background: rgba(255, 69, 58, 0.16); border-color: var(--danger); color: #ffb4b0; }
button.danger:hover { background: rgba(255, 69, 58, 0.28); }
button.secondary { background: transparent; }
button.cell-btn { background: none; border: none; padding: 2px; margin: 0; font-size: 1.1rem; cursor: pointer; line-height: 1; }

.top {
  display: flex; align-items: center; gap: 14px; padding: 12px 24px; border-bottom: 1px solid var(--line);
  background: rgba(10, 12, 24, 0.6); position: sticky; top: 0; z-index: 5; backdrop-filter: blur(14px);
  flex-wrap: wrap;
}
.brand { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 15px; white-space: nowrap; }
.logo { width: 26px; height: 12px; border-radius: 4px; background: linear-gradient(90deg, var(--accent), #8f7cff);
        box-shadow: 0 0 14px rgba(90, 200, 250, 0.5); flex-shrink: 0; }
nav {
  display: flex; gap: 2px; flex: 1 1 100%; order: 3;
  overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none;
}
nav::-webkit-scrollbar { display: none; }
nav a {
  color: var(--dim); text-decoration: none; padding: 9px 13px; border-radius: 9px; font-size: 13px;
  white-space: nowrap; flex-shrink: 0;
}
nav a.active, nav a:hover { background: var(--panel-2); color: var(--text); }
.user { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--dim); white-space: nowrap;
        margin-left: auto; order: 2; }
.user a { color: var(--dim); text-decoration: none; padding: 4px 2px; }
.user a:hover { color: var(--text); }

main { padding: 20px 24px 60px; max-width: 920px; margin: 0 auto; }

.card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px; margin-bottom: 16px; }
.card > button:first-child { margin-top: 0; }

.flash { background: rgba(48, 209, 88, 0.12); border: 1px solid rgba(48, 209, 88, 0.4); color: #8af0a6;
         padding: 10px 14px; border-radius: 10px; font-size: 13px; margin-bottom: 16px; }

code { background: rgba(0, 0, 0, 0.3); padding: 2px 6px; border-radius: 6px; font-size: 12px; }

table { width: 100%; border-collapse: collapse; font-size: 13px; }
td, th { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--line); word-break: break-word; }
tr:last-child td { border-bottom: none; }
.row-form { display: inline; }

.checklist { display: flex; flex-wrap: wrap; gap: 6px 16px; }
.checklist label { display: flex; align-items: center; gap: 6px; margin: 0; font-weight: 400; color: var(--text); font-size: 13px; }

table.matrix { white-space: nowrap; }
table.matrix th, table.matrix td { text-align: center; padding: 4px 8px; }
table.matrix th:first-child, table.matrix td:first-child { text-align: left; position: sticky; left: 0; background: #11131f; }
table.matrix form { margin: 0; }

.chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; padding: 3px 10px; border-radius: 99px;
        background: var(--panel-2); border: 1px solid var(--line); color: var(--dim); }
.chip .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--dim); }
.chip.ok .dot { background: var(--success); }

.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 14px; }
@media (max-width: 600px) { .grid2 { grid-template-columns: 1fr; } }

@media (max-width: 760px) {
  .top { padding: 10px 14px; gap: 8px; }
  main { padding: 14px 14px 40px; }
  .card { padding: 14px; }
  h1 { font-size: 18px; }
  table { font-size: 12.5px; }
  td, th { padding: 8px 4px; }
  button[type=submit] { width: 100%; }
  .row-form button[type=submit] { width: auto; }  /* kleine Löschen-Buttons in Tabellenzeilen ausnehmen */
  .checklist { gap: 10px 14px; }
}

/* Große Finger statt Mauszeiger: etwas mehr Platz, keine Hover-Zustände nötig */
@media (hover: none) and (pointer: coarse) {
  nav a { padding: 10px 14px; }
  button { min-height: 44px; }
  .checklist label { padding: 2px 0; }
}
"""


def layout(title: str, body: str, *, user: dict | None = None, active: str = "", flash: str = "") -> str:
    nav_html = "".join(
        f'<a href="{path}" class="{"active" if path == active else ""}">{label}</a>'
        for path, label in NAV
    )
    user_html = ""
    if user:
        user_html = f'<div class="user"><span>{esc(user["name"])}</span><a href="/logout">Abmelden</a></div>'
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} – TheWashBot</title>
<style>{DASHBOARD_CSS}</style></head><body>
<div class="top">
  <div class="brand"><span class="logo"></span>TheWashBot</div>
  <nav>{nav_html}</nav>
  {user_html}
</div>
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


def channel_checklist(guild: discord.Guild, name: str, selected_ids: list[int],
                      *, types=(discord.ChannelType.text,)) -> str:
    rows = []
    for ch in sorted((c for c in guild.channels if c.type in types), key=lambda c: c.position):
        checked = " checked" if ch.id in selected_ids else ""
        rows.append(f'<label><input type="checkbox" name="{name}" value="{ch.id}"{checked}> #{esc(ch.name)}</label>')
    inner = "".join(rows) or '<span class="hint">Keine passenden Kanäle vorhanden.</span>'
    return f'<div class="checklist">{inner}</div>'


def visible_roles(guild: discord.Guild) -> list[discord.Role]:
    """Echte Mitglieder-Rollen für die Sichtbarkeits-Matrix — ohne @everyone und ohne
    bot-eigene Integrations-Rollen (TheWashbot, Jellyfin, …), die kann man eh nicht manuell vergeben."""
    return sorted((r for r in guild.roles if not r.is_default() and not r.managed), key=lambda r: -r.position)


def role_can_view(channel, role: discord.Role, everyone: discord.Role) -> bool:
    """Könnte ein Mitglied, das nur @everyone + diese eine Rolle hat, den Kanal/die Kategorie
    sehen? (Admin-Rechte/Owner-Status bleiben hier bewusst außen vor — reine Rollen-Perspektive.)"""
    base = everyone.permissions.value | role.permissions.value
    VIEW = 1 << 10
    for target in (everyone, role):
        ow = channel.overwrites_for(target)
        if ow.view_channel is False:
            base &= ~VIEW
        elif ow.view_channel is True:
            base |= VIEW
    return bool(base & VIEW)


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
            web.get("/aion", self.page_aion), web.post("/aion", self.post_aion),
            web.post("/aion/token", self.post_aion_token),
            web.get("/warnings", self.page_warnings), web.post("/warnings", self.post_warnings),
            web.get("/automod", self.page_automod), web.post("/automod", self.post_automod),
            web.get("/community", self.page_community), web.post("/community", self.post_community),
            web.get("/level", self.page_level), web.post("/level", self.post_level),
            web.post("/level/no-xp", self.post_level_no_xp),
            web.post("/level/reward/add", self.post_level_reward_add),
            web.post("/level/reward/{level}/delete", self.post_level_reward_delete),
            web.get("/announcements", self.page_announcements),
            web.post("/announcements/add", self.post_announcement_add),
            web.post("/announcements/{id}/delete", self.post_announcement_delete),
            web.get("/commands", self.page_commands),
            web.post("/commands/add", self.post_command_add),
            web.post("/commands/{name}/delete", self.post_command_delete),
            web.get("/notifications", self.page_notifications),
            web.post("/notifications/{id}/toggle", self.post_notification_toggle),
            web.post("/notifications/{id}/delete", self.post_notification_delete),
            web.get("/matrix", self.page_matrix),
            web.post("/matrix/toggle", self.post_matrix_toggle),
            web.post("/api/aion/fight", self.api_aion_fight),
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

        def fmt(v):
            if not v:
                return "–"
            ch = guild.get_channel(v)
            if ch:
                return f"#{esc(ch.name)}"
            role = guild.get_role(v)
            return esc(role.name) if role else f"({v})"

        def onoff(flag: bool) -> str:
            return '<span class="chip ok"><span class="dot"></span>an</span>' if flag else \
                '<span class="chip"><span class="dot"></span>aus</span>'

        chips = (
            f'<span class="chip ok"><span class="dot"></span>{esc(guild.name)}</span>'
            f'<span class="chip ok"><span class="dot"></span>{len(guild.members)} Mitglieder</span>'
        )
        rows = [
            ("Log-Kanal", fmt(db.get_int(guild.id, "log_channel"))),
            ("Willkommenskanal", fmt(db.get_int(guild.id, "welcome_channel"))),
            ("Autorolle", fmt(db.get_int(guild.id, "autorole"))),
            ("Temp-Voice", fmt(db.get_int(guild.id, "tempvoice_hub"))),
            ("DJ-Rolle", fmt(db.get_int(guild.id, "dj_role"))),
            ("Automod", onoff(bool(db.get(guild.id, "automod_banned_words")) or db.get_bool(guild.id, "automod_caps")
                              or db.get_bool(guild.id, "automod_link_filter") or bool(db.get_int(guild.id, "automod_emoji_limit")))),
            ("Levelsystem", onoff(db.get_bool(guild.id, "level_system_enabled"))),
            ("Starboard", fmt(db.get_int(guild.id, "starboard_channel"))),
            ("Bump-Erinnerung", fmt(db.get_int(guild.id, "bump_channel"))),
            ("Geburtstage", fmt(db.get_int(guild.id, "birthday_channel"))),
        ]
        body = f'<div class="chips" style="margin-bottom:16px">{chips}</div>'
        body += '<div class="card"><table>' + "".join(
            f"<tr><td>{esc(label)}</td><td>{value}</td></tr>" for label, value in rows
        ) + "</table></div>"
        body += (
            '<div class="card"><h2>Hallo 👋</h2><p>Links oben die Bereiche. Neue '
            "GitHub-/YouTube-Benachrichtigungen legst du weiterhin direkt in Discord an "
            '(🔔-Button im Einstellungen-Kanal) — hier kannst du bestehende an/ausschalten oder '
            "löschen. Giveaways, der Server-Status und die Verifizierungs-Ersteinrichtung bleiben "
            "ebenfalls in Discord, wegen Vorschau/Bestätigung bzw. weil sie Root-Zugriff auf den "
            "Server brauchen.</p></div>"
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
<form method="post">
<div class="card">
  <h2>Moderation</h2>
  <label>Log-Kanal</label>
  <select name="log_channel">{channel_options(guild, db.get_int(guild.id, "log_channel"))}</select>
  <div class="hint">Gelöschte/bearbeitete Nachrichten, Beitritte, Bans, …</div>
  <label><input type="checkbox" name="antispam" {"checked" if db.get_bool(guild.id, "antispam", True) else ""}> Spamschutz aktiv</label>
  <label><input type="checkbox" name="allow_invites" {"checked" if db.get_bool(guild.id, "allow_invites") else ""}> Einladungslinks erlauben</label>
</div>
<div class="card">
  <h2>Willkommen</h2>
  <label>Willkommenskanal</label>
  <select name="welcome_channel">{channel_options(guild, db.get_int(guild.id, "welcome_channel"))}</select>
  <label>Begrüßungstext</label>
  <textarea name="welcome_text">{esc(db.get(guild.id, "welcome_text", ""))}</textarea>
  <div class="hint">Platzhalter: <code>{{user}}</code> <code>{{name}}</code> <code>{{server}}</code> <code>{{anzahl}}</code></div>
  <label>Autorolle (z. B. „Neuling“, sofort bei Beitritt)</label>
  <select name="autorole">{role_options(guild, db.get_int(guild.id, "autorole"))}</select>
</div>
<div class="card">
  <h2>Musik &amp; Voice</h2>
  <label>DJ-Rolle (leer = alle dürfen steuern)</label>
  <select name="dj_role">{role_options(guild, db.get_int(guild.id, "dj_role"))}</select>
  <label>Temp-Voice-Kanal</label>
  <select name="tempvoice_hub">{channel_options(guild, db.get_int(guild.id, "tempvoice_hub"), types=(discord.ChannelType.voice,))}</select>
  <div class="hint">Neu anlegen/ändern geht genauer per <code>/setup temp-voice</code> in Discord.</div>
  <button type="submit">Speichern</button>
</div>
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

    # ---- AION-2-Duplikatfilter -------------------------------------------------------------------

    async def page_aion(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        aion = self.bot.get_cog("AionDedup")
        token = self.db.get(guild.id, "aion_api_token")
        token_html = f"<code>{esc(token)}</code>" if token else "noch nicht erzeugt"
        body = f"""
<form method="post" class="card">
  <h2>Ziel-Kanal</h2>
  <select name="channel">{channel_options(guild, self.db.get_int(guild.id, "aion_channel"))}</select>
  <div class="hint">Leer lassen (erster Eintrag) zum Abschalten.</div>
  <button type="submit">Speichern</button>
</form>
<form method="post" action="/aion/token" class="card">
  <h2>API-Variante (braucht eine DeckBar-Änderung)</h2>
  <div class="hint">Endpunkt: <code>{esc(aion.api_url())}</code></div>
  <div class="hint">Token: {token_html}</div>
  <button type="submit">Neuen Token erzeugen (macht den alten ungültig)</button>
</form>"""
        return web.Response(
            text=layout("AION-2-Duplikatfilter", body, user=self._user_ctx(request), active="/aion",
                        flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_aion(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        channel_id = form.get("channel")
        await self.db.set(guild.id, "aion_channel", int(channel_id) if channel_id else None)
        return self._redirect("/aion", "Gespeichert.")

    async def post_aion_token(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        import secrets

        await self.db.set(guild.id, "aion_api_token", secrets.token_urlsafe(24))
        return self._redirect("/aion", "Neuer Token erzeugt.")

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

    # ---- Automod ---------------------------------------------------------------------------------

    async def page_automod(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        db = self.db
        body = f"""
<form method="post">
<div class="card">
  <h2>Wortfilter &amp; Links</h2>
  <label>Verbotene Wörter (durch Komma getrennt)</label>
  <textarea name="banned_words">{esc(db.get(guild.id, "automod_banned_words", ""))}</textarea>
  <label><input type="checkbox" name="link_filter" {"checked" if db.get_bool(guild.id, "automod_link_filter") else ""}> Nur Links zu erlaubten Domains zulassen</label>
  <label>Erlaubte Domains (durch Komma getrennt)</label>
  <textarea name="allowed_domains">{esc(db.get(guild.id, "automod_allowed_domains", ""))}</textarea>
</div>
<div class="card">
  <h2>Großbuchstaben &amp; Emoji-Spam</h2>
  <label><input type="checkbox" name="caps" {"checked" if db.get_bool(guild.id, "automod_caps") else ""}> Nachrichten mit zu vielen Großbuchstaben entfernen</label>
  <label>Emoji-Grenze pro Nachricht (0 = aus)</label>
  <input type="number" min="0" name="emoji_limit" value="{db.get_int(guild.id, 'automod_emoji_limit') or 0}">
  <button type="submit">Speichern</button>
</div>
</form>
<div class="card hint">Jeder Automod-Verstoß zählt zusätzlich als automatische Verwarnung (zählt in
  Richtung der Schwelle unter „Verwarnungen“) — Moderator ist dann der Bot selbst.</div>"""
        return web.Response(
            text=layout("Automod", body, user=self._user_ctx(request), active="/automod", flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_automod(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        await self.db.set(guild.id, "automod_banned_words", str(form.get("banned_words") or "").strip() or None)
        await self.db.set(guild.id, "automod_link_filter", "link_filter" in form)
        await self.db.set(guild.id, "automod_allowed_domains", str(form.get("allowed_domains") or "").strip() or None)
        await self.db.set(guild.id, "automod_caps", "caps" in form)
        try:
            emoji_limit = max(0, int(form.get("emoji_limit") or 0))
        except ValueError:
            emoji_limit = 0
        await self.db.set(guild.id, "automod_emoji_limit", emoji_limit or None)
        return self._redirect("/automod", "Gespeichert.")

    # ---- Community: Zähler, Starboard, Geburtstage, Bump-Erinnerung ------------------------------

    async def page_community(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        db = self.db
        body = f"""
<form method="post">
<div class="card">
  <h2>Statistik-Kanäle</h2>
  <div class="grid2">
    <div>
      <label>Mitglieder-Zähler (Sprachkanal)</label>
      <select name="member_count_channel">{channel_options(guild, db.get_int(guild.id, "member_count_channel"), types=(discord.ChannelType.voice,))}</select>
    </div>
    <div>
      <label>Bots-Zähler (Sprachkanal)</label>
      <select name="bots_count_channel">{channel_options(guild, db.get_int(guild.id, "bots_count_channel"), types=(discord.ChannelType.voice,))}</select>
    </div>
  </div>
</div>
<div class="card">
  <h2>Starboard</h2>
  <div class="grid2">
    <div>
      <label>Highlight-Kanal</label>
      <select name="starboard_channel">{channel_options(guild, db.get_int(guild.id, "starboard_channel"))}</select>
    </div>
    <div>
      <label>Schwelle (⭐, ohne den Stern des Autors)</label>
      <input type="number" min="1" name="starboard_threshold" value="{db.get_int(guild.id, 'starboard_threshold') or 3}">
    </div>
  </div>
</div>
<div class="card">
  <h2>Geburtstage</h2>
  <label>Kanal für die tägliche Gratulation (9 Uhr)</label>
  <select name="birthday_channel">{channel_options(guild, db.get_int(guild.id, "birthday_channel"))}</select>
  <div class="hint">Mitglieder tragen ihren eigenen Geburtstag per <code>/geburtstag-setzen</code> ein.</div>
</div>
<div class="card">
  <h2>Bump-Erinnerung (Disboard)</h2>
  <div class="grid2">
    <div>
      <label>Kanal, in dem ihr <code>/bump</code> nutzt</label>
      <select name="bump_channel">{channel_options(guild, db.get_int(guild.id, "bump_channel"))}</select>
    </div>
    <div>
      <label>Rolle, die gepingt wird (optional)</label>
      <select name="bump_role">{role_options(guild, db.get_int(guild.id, "bump_role"))}</select>
    </div>
  </div>
  <button type="submit">Speichern</button>
</div>
</form>"""
        return web.Response(
            text=layout("Community", body, user=self._user_ctx(request), active="/community", flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_community(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        await self.db.set(guild.id, "member_count_channel", form.get("member_count_channel") or None)
        await self.db.set(guild.id, "bots_count_channel", form.get("bots_count_channel") or None)
        await self.db.set(guild.id, "starboard_channel", form.get("starboard_channel") or None)
        try:
            starboard_threshold = max(1, int(form.get("starboard_threshold") or 3))
        except ValueError:
            starboard_threshold = 3
        await self.db.set(guild.id, "starboard_threshold", starboard_threshold)
        await self.db.set(guild.id, "birthday_channel", form.get("birthday_channel") or None)
        await self.db.set(guild.id, "bump_channel", form.get("bump_channel") or None)
        await self.db.set(guild.id, "bump_role", form.get("bump_role") or None)
        stats_cog = self.bot.get_cog("Stats")
        if stats_cog:
            await stats_cog.update_guild(guild)
        return self._redirect("/community", "Gespeichert.")

    # ---- Levelsystem -------------------------------------------------------------------------------

    async def page_level(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        db = self.db
        rewards = await self.db.fetchall(
            "SELECT level, role_id FROM level_rewards WHERE guild_id=? ORDER BY level", (guild.id,)
        )
        def reward_role_name(role_id: int) -> str:
            role = guild.get_role(role_id)
            return esc(role.name) if role else "(gelöscht)"

        reward_rows = "".join(
            f"""<tr><td>{r['level']}</td><td>{reward_role_name(r['role_id'])}</td>
  <td><form class="row-form" method="post" action="/level/reward/{r['level']}/delete"
        onsubmit="return confirm('Belohnung für Level {r['level']} löschen?')">
    <button type="submit" class="danger">Löschen</button></form></td></tr>"""
            for r in rewards
        ) or '<tr><td colspan="3" class="hint">Noch keine Rollen-Belohnungen.</td></tr>'
        no_xp_channels = db.get_ids(guild.id, "level_no_xp_channels")
        no_xp_roles = db.get_ids(guild.id, "level_no_xp_roles")
        body = f"""
<form method="post" class="card">
  <h2>Grundeinstellungen</h2>
  <label><input type="checkbox" name="enabled" {"checked" if db.get_bool(guild.id, "level_system_enabled") else ""}> Levelsystem an</label>
  <div class="grid2">
    <div>
      <label>Kanal für Level-Up-Nachrichten (leer = im Nachrichten-Kanal)</label>
      <select name="announce_channel">{channel_options(guild, db.get_int(guild.id, "level_announce_channel"))}</select>
    </div>
    <div>
      <label>XP pro Minute Sprachzeit (0 = aus)</label>
      <input type="number" min="0" name="voice_xp" value="{db.get_int(guild.id, 'voice_xp_per_minute') or 0}">
    </div>
  </div>
  <button type="submit">Speichern</button>
</form>
<form method="post" action="/level/no-xp" class="card">
  <h2>Kanäle &amp; Rollen ohne XP</h2>
  <label>Kanäle</label>
  {channel_checklist(guild, "no_xp_channels", no_xp_channels)}
  <label>Rollen</label>
  {role_checklist(guild, "no_xp_roles", no_xp_roles)}
  <button type="submit">Speichern</button>
</form>
<div class="card">
  <h2>Rollen-Belohnungen (kumulativ)</h2>
  <table><tr><th>Level</th><th>Rolle</th><th></th></tr>{reward_rows}</table>
  <form method="post" action="/level/reward/add" class="grid2">
    <div><label>Ab Level</label><input type="number" min="1" name="level" required></div>
    <div><label>Rolle</label><select name="role">{role_options(guild, None)}</select></div>
    <button type="submit">Hinzufügen</button>
  </form>
</div>"""
        return web.Response(
            text=layout("Levelsystem", body, user=self._user_ctx(request), active="/level", flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_level(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        await self.db.set(guild.id, "level_system_enabled", "enabled" in form)
        await self.db.set(guild.id, "level_announce_channel", form.get("announce_channel") or None)
        try:
            voice_xp = max(0, int(form.get("voice_xp") or 0))
        except ValueError:
            voice_xp = 0
        await self.db.set(guild.id, "voice_xp_per_minute", voice_xp or None)
        return self._redirect("/level", "Gespeichert.")

    async def post_level_no_xp(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        await self.db.set(guild.id, "level_no_xp_channels", ",".join(form.getall("no_xp_channels", [])) or None)
        await self.db.set(guild.id, "level_no_xp_roles", ",".join(form.getall("no_xp_roles", [])) or None)
        return self._redirect("/level", "Gespeichert.")

    async def post_level_reward_add(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        try:
            level = int(form.get("level") or 0)
        except ValueError:
            level = 0
        role_id = form.get("role")
        if level < 1 or not role_id:
            return self._redirect("/level", "Level und Rolle angeben.")
        role = guild.get_role(int(role_id))
        if role and (role.managed or role >= guild.me.top_role):
            return self._redirect("/level", f"{role.name} kann ich nicht vergeben (muss unter meiner eigenen Rolle stehen).")
        await self.db.execute(
            "INSERT INTO level_rewards (guild_id, level, role_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, level) DO UPDATE SET role_id=excluded.role_id",
            (guild.id, level, int(role_id)),
        )
        return self._redirect("/level", "Belohnung gespeichert.")

    async def post_level_reward_delete(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        level = int(request.match_info["level"])
        await self.db.execute("DELETE FROM level_rewards WHERE guild_id=? AND level=?", (guild.id, level))
        return self._redirect("/level", "Belohnung gelöscht.")

    # ---- Zeitgesteuerte Ankündigungen ---------------------------------------------------------

    async def page_announcements(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        rows = await self.db.fetchall(
            "SELECT id, channel_id, text, interval_minutes FROM scheduled WHERE guild_id=? ORDER BY id", (guild.id,)
        )
        table_rows = "".join(
            f"""<tr><td>{("#" + esc(guild.get_channel(r['channel_id']).name)) if guild.get_channel(r['channel_id']) else "–"}</td>
  <td>{esc(r['text'][:80])}</td><td>alle {r['interval_minutes']} Min.</td>
  <td><form class="row-form" method="post" action="/announcements/{r['id']}/delete"
        onsubmit="return confirm('Ankündigung löschen?')"><button type="submit" class="danger">Löschen</button></form></td></tr>"""
            for r in rows
        ) or '<tr><td colspan="4" class="hint">Noch keine Ankündigung eingerichtet.</td></tr>'
        body = f"""
<div class="card">
  <h2>Eingerichtet</h2>
  <table><tr><th>Kanal</th><th>Text</th><th>Intervall</th><th></th></tr>{table_rows}</table>
</div>
<form method="post" action="/announcements/add" class="card">
  <h2>Neu anlegen</h2>
  <label>Kanal</label>
  <select name="channel">{channel_options(guild, None)}</select>
  <label>Text</label>
  <textarea name="text" required></textarea>
  <label>Intervall (Minuten, mindestens 5)</label>
  <input type="number" min="5" name="interval" value="60" required>
  <button type="submit">Anlegen</button>
</form>"""
        return web.Response(
            text=layout("Ankündigungen", body, user=self._user_ctx(request), active="/announcements",
                        flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_announcement_add(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        channel_id = form.get("channel")
        text = str(form.get("text") or "").strip()
        if not channel_id or not text:
            return self._redirect("/announcements", "Kanal und Text angeben.")
        try:
            interval = max(5, int(form.get("interval") or 60))
        except ValueError:
            interval = 60
        await self.db.execute(
            "INSERT INTO scheduled (guild_id, channel_id, text, interval_minutes, next_run) VALUES (?, ?, ?, ?, ?)",
            (guild.id, int(channel_id), text, interval, time.time()),
        )
        return self._redirect("/announcements", "Angelegt.")

    async def post_announcement_delete(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        ann_id = int(request.match_info["id"])
        await self.db.execute("DELETE FROM scheduled WHERE id=? AND guild_id=?", (ann_id, guild.id))
        return self._redirect("/announcements", "Gelöscht.")

    # ---- Eigene Befehle ------------------------------------------------------------------------

    async def page_commands(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        rows = await self.db.fetchall(
            "SELECT name, response FROM custom_commands WHERE guild_id=? ORDER BY name", (guild.id,)
        )
        table_rows = "".join(
            f"""<tr><td><code>!{esc(r['name'])}</code></td><td>{esc(r['response'][:80])}</td>
  <td><form class="row-form" method="post" action="/commands/{urllib.parse.quote(r['name'])}/delete"
        onsubmit="return confirm('!{esc(r['name'])} löschen?')"><button type="submit" class="danger">Löschen</button></form></td></tr>"""
            for r in rows
        ) or '<tr><td colspan="3" class="hint">Noch keine eigenen Befehle.</td></tr>'
        body = f"""
<div class="card">
  <h2>Eingerichtet</h2>
  <table><tr><th>Befehl</th><th>Antwort</th><th></th></tr>{table_rows}</table>
</div>
<form method="post" action="/commands/add" class="card">
  <h2>Neu anlegen (überschreibt einen gleichnamigen Befehl)</h2>
  <label>Name (ohne „!“)</label>
  <input type="text" name="name" pattern="[a-z0-9_-]{{1,32}}" required>
  <label>Antwort</label>
  <textarea name="response" required></textarea>
  <div class="hint">Platzhalter: <code>{{user}}</code> (Erwähnung), <code>{{server}}</code></div>
  <button type="submit">Speichern</button>
</form>"""
        return web.Response(
            text=layout("Eigene Befehle", body, user=self._user_ctx(request), active="/commands",
                        flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_command_add(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        form = await request.post()
        name = str(form.get("name") or "").strip().lower().lstrip("!")
        response = str(form.get("response") or "").strip()
        import re

        if not re.match(r"^[a-z0-9_-]{1,32}$", name) or not response:
            return self._redirect("/commands", "Ungültiger Name oder leere Antwort.")
        await self.db.execute(
            "INSERT INTO custom_commands (guild_id, name, response) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, name) DO UPDATE SET response=excluded.response",
            (guild.id, name, response),
        )
        return self._redirect("/commands", f"!{name} gespeichert.")

    async def post_command_delete(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        name = urllib.parse.unquote(request.match_info["name"])
        await self.db.execute("DELETE FROM custom_commands WHERE guild_id=? AND name=?", (guild.id, name))
        return self._redirect("/commands", "Gelöscht.")

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

    # ---- Sichtbarkeits-Matrix ------------------------------------------------------------------

    async def page_matrix(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        _, guild = result
        everyone = guild.default_role
        roles = visible_roles(guild)

        def cell(channel, role) -> str:
            can_view = role_can_view(channel, role, everyone)
            return (
                f'<form method="post" action="/matrix/toggle">'
                f'<input type="hidden" name="channel_id" value="{channel.id}">'
                f'<input type="hidden" name="role_id" value="{role.id}">'
                f'<input type="hidden" name="allow" value="{0 if can_view else 1}">'
                f'<button type="submit" class="cell-btn" title="{esc(channel.name)} / {esc(role.name)}">'
                f'{"✅" if can_view else "⬜"}</button></form>'
            )

        def row(channel, indent: bool) -> str:
            name = f'↳ #{esc(channel.name)}' if indent else f'<strong>📁 {esc(channel.name)}</strong>'
            cells = "".join(f"<td>{cell(channel, r)}</td>" for r in roles)
            return f"<tr><td>{name}</td>{cells}</tr>"

        rows = []
        categories = sorted((c for c in guild.categories), key=lambda c: c.position)
        for cat in categories:
            rows.append(row(cat, indent=False))
            children = sorted(
                (c for c in cat.channels if c.type in (discord.ChannelType.text, discord.ChannelType.voice)),
                key=lambda c: c.position,
            )
            rows.extend(row(c, indent=True) for c in children)
        uncategorized = sorted(
            (c for c in guild.channels if c.category is None
             and c.type in (discord.ChannelType.text, discord.ChannelType.voice)),
            key=lambda c: c.position,
        )
        if uncategorized:
            rows.append('<tr><td><strong>📁 (ohne Kategorie)</strong></td>' + "<td></td>" * len(roles) + "</tr>")
            rows.extend(row(c, indent=True) for c in uncategorized)

        header = "<th>Kanal</th>" + "".join(f"<th>{esc(r.name)}</th>" for r in roles)
        body = (
            '<div class="card hint">✅ = diese Rolle sieht den Kanal/die Kategorie gerade (nur Sehen, '
            "keine anderen Rechte). Klick zum Umschalten — wirkt sofort auf dem Server. 📁-Zeilen sind "
            "Kategorien, darunter ihre Kanäle.</div>"
            f'<div class="card" style="overflow-x:auto"><table class="matrix"><tr>{header}</tr>{"".join(rows)}</table></div>'
        )
        return web.Response(
            text=layout("Sichtbarkeit", body, user=self._user_ctx(request), active="/matrix", flash=self._flash(request)),
            content_type="text/html",
        )

    async def post_matrix_toggle(self, request: web.Request) -> web.Response:
        result = await self.require_admin(request)
        if isinstance(result, web.Response):
            return result
        member, guild = result
        form = await request.post()
        try:
            channel_id = int(form["channel_id"])
            role_id = int(form["role_id"])
            allow = form["allow"] == "1"
        except (KeyError, ValueError):
            return self._redirect("/matrix", "Ungültige Anfrage.")
        channel = guild.get_channel(channel_id)
        role = guild.get_role(role_id)
        if not channel or not role:
            return self._redirect("/matrix", "Kanal oder Rolle nicht gefunden.")
        try:
            await channel.set_permissions(
                role, view_channel=allow, reason=f"Dashboard: {member}"
            )
        except discord.HTTPException as e:
            return self._redirect("/matrix", f"Fehler: {e}")
        return self._redirect("/matrix", f"{'Sichtbar' if allow else 'Versteckt'}: {channel.name} / {role.name}")

    # ---- AION-2-API (eigener Bearer-Token, kein Discord-Login) -----------------------------

    async def api_aion_fight(self, request: web.Request) -> web.Response:
        aion = self.bot.get_cog("AionDedup")
        if aion is None:
            return web.json_response({"error": "AION-Modul nicht geladen"}, status=503)
        return await aion.handle_api_fight(request)


async def setup(bot):
    await bot.add_cog(Dashboard(bot))
