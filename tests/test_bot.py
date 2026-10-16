"""Offline-Tests: Befehle laden, Datenbank, Warteschlangen-Logik, Quellen-Erkennung."""
import asyncio
import contextlib
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bot.__main__ import Bot
from bot.db import Database
from bot.sources import SPOTIFY_RE, Track, _find_key, is_url


def run(coro):
    return asyncio.run(coro)


def test_all_commands_register():
    async def go():
        with tempfile.TemporaryDirectory() as d:
            bot = Bot(Database(Path(d) / "t.sqlite3"), sync=False)
            await bot.setup_hook()
            names = sorted(c.qualified_name for c in bot.tree.walk_commands())
            # Reminders.check_due und Notifications.poll_feeds scheitern hier erwartbar an
            # wait_until_ready() (kein echtes Login in diesem Test) — Tasks sauber abwarten statt
            # "exception was never retrieved".
            for cog_name, loop_name in [
                ("Reminders", "check_due"), ("Notifications", "poll_feeds"),
                ("Bump", "check_due"), ("Stats", "refresh"), ("Birthdays", "check"),
                ("Announcements", "check_due"), ("VoiceStats", "tick"),
            ]:
                loop = getattr(bot.get_cog(cog_name), loop_name)
                task = loop.get_task()
                loop.cancel()
                if task:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await task
            await bot.get_cog("DpsWebhook").runner.cleanup()
            await bot.get_cog("Dashboard").cog_unload()
            await bot.get_cog("Notifications").manager.close()
            await bot.db.close()
            await bot.get_cog("Music").session.close()
            return names
    names = run(go())
    for expected in ["play", "radio", "skip", "stop", "warteschlange", "lautstaerke", "wiederholen",
                     "setup log-kanal", "setup temp-voice", "setup panel", "setup management-kanal",
                     "setup verifizierung", "setup regeln-text", "setup verwarnungen", "rollen-panel", "timeout",
                     "warn", "warnungen", "warnung-loeschen", "kick", "ban", "unban",
                     "erinnere", "erinnerungen", "erinnerung-loeschen", "setup dps-webhook", "setup dashboard", "aion",
                     "setup tickets", "ticket-panel", "setup bump", "setup mitglieder-zaehler", "setup starboard",
                     "setup bots-zaehler", "umfrage", "geburtstag-setzen", "geburtstag-loeschen", "geburtstag",
                     "setup geburtstage", "embed-erstellen", "befehl-erstellen", "befehl-loeschen", "befehle",
                     "ankuendigung-erstellen", "ankuendigungen", "ankuendigung-loeschen",
                     "sprachzeit", "setup sprachzeit-xp",
                     "vc name", "vc sperren", "aufraeumen nachrichten", "aufraeumen bots", "aufraeumen bericht"]:
        assert expected in names, expected
    # Discord: höchstens 100 Top-Level-Befehle, Namen kleingeschrieben, ≤ 32 Zeichen
    for n in names:
        for part in n.split():
            assert part == part.lower() and len(part) <= 32


def test_db_roundtrip():
    async def go():
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "t.sqlite3")
            await db.connect()
            await db.set(1, "log_channel", 42)
            await db.set(1, "antispam", False)
            await db.add_temp_channel(5, 1, 9)
            await db.close()
            db2 = Database(Path(d) / "t.sqlite3")
            await db2.connect()
            r = (db2.get_int(1, "log_channel"), db2.get_bool(1, "antispam", True),
                 await db2.temp_channel_owner(5))
            await db2.set(1, "log_channel", None)
            r += (db2.get(1, "log_channel"),)
            await db2.close()
            return r
    assert run(go()) == (42, False, 9, None)


def make_player(tracks, loop="aus"):
    from bot.cogs.music import Player
    p = Player.__new__(Player)
    p.queue, p.current, p.loop, p._skipped = list(tracks), None, loop, False
    return p


def t(name):
    return Track(title=name, query=name, requester_id=1)


def test_queue_order_and_loops():
    a, b, c = t("a"), t("b"), t("c")
    p = make_player([a, b, c])
    seq = []
    for _ in range(3):
        p.current = p._next(); seq.append(p.current.title)
    assert seq == ["a", "b", "c"] and p._next() is None

    p = make_player([a, b], loop="song")
    p.current = p._next()
    assert p._next() is a            # Song wiederholen
    p._skipped = True
    assert p._next() is b            # Überspringen beendet die Wiederholung des Songs

    p = make_player([a, b], loop="liste")
    seq = []
    for _ in range(5):
        p.current = p._next(); seq.append(p.current.title)
    assert seq == ["a", "b", "a", "b", "a"]


def test_source_detection():
    assert is_url("https://youtu.be/x") and not is_url("never gonna give you up")
    m = SPOTIFY_RE.search("https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M?si=1")
    assert m.group(1) == "playlist" and m.group(2) == "37i9dQZF1DXcBWIGoYBM5M"
    assert _find_key({"a": [{"b": {"trackList": [1]}}]}, "trackList") == [1]


def test_settings_panel_persistent_has_stable_custom_ids():
    from bot.cogs.settings import SettingsPanel

    cog = type("FakeCog", (), {})()
    view = SettingsPanel(cog, guild=object(), persistent=True)
    assert len(view.children) == 18  # kein Schließen-Button im persistenten Modus
    assert all(item.custom_id for item in view.children)

    ephemeral = SettingsPanel(cog, guild=object(), persistent=False)
    assert len(ephemeral.children) == 19  # inkl. Schließen


def test_reminder_parsing():
    from zoneinfo import ZoneInfo

    from bot.cogs.reminders import parse_when

    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 14, 12, 0, tzinfo=tz)

    assert parse_when("2h", now) == now + timedelta(hours=2)
    assert parse_when("30m", now) == now + timedelta(minutes=30)
    assert parse_when("1d2h", now) == now + timedelta(days=1, hours=2)

    assert parse_when("18:30", now) == now.replace(hour=18, minute=30)
    assert parse_when("08:00", now) == now.replace(hour=8, minute=0) + timedelta(days=1)  # schon vorbei -> morgen

    assert parse_when("24.12. 18:00", now) == datetime(2026, 12, 24, 18, 0, tzinfo=tz)
    assert parse_when("1.1. 10:00", now) == datetime(2027, 1, 1, 10, 0, tzinfo=tz)  # schon vorbei -> nächstes Jahr
    assert parse_when("24.12.2025 18:00", now) == datetime(2025, 12, 24, 18, 0, tzinfo=tz)  # Jahr explizit, kein Shift

    with pytest.raises(Exception):
        parse_when("irgendwann", now)


def test_feeds_migration_on_existing_db():
    """Simuliert eine schon laufende Installation: feeds-Tabelle existiert noch ohne die neuen
    Spalten (events/filters/enabled/state/error) — Database.connect() muss sie nachrüsten,
    ohne bestehende Zeilen zu verlieren."""
    import aiosqlite

    async def go():
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.sqlite3"
            conn = await aiosqlite.connect(path)
            await conn.execute(
                "CREATE TABLE feeds (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
                "channel_id INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, "
                "message TEXT NOT NULL DEFAULT '', last_id TEXT)"
            )
            await conn.execute(
                "INSERT INTO feeds (guild_id, channel_id, kind, source) VALUES (1, 2, 'github', 'o/r')"
            )
            await conn.commit()
            await conn.close()

            db = Database(path)
            await db.connect()
            row = await db.fetchone("SELECT * FROM feeds WHERE source='o/r'")
            await db.close()
            return row

    row = run(go())
    assert row["guild_id"] == 1 and row["source"] == "o/r"
    assert row["events"] == "[]" and row["filters"] == "{}" and row["enabled"] == 1 and row["state"] == "{}"


def test_github_provider_dedup_and_transitions():
    from bot.notify.github import GitHubProvider

    provider = GitHubProvider()
    responses: dict[str, tuple[int, object]] = {}

    async def fake_get(path, **params):
        return responses[path.split("/")[-1]]

    provider._get = fake_get  # keine echten HTTP-Aufrufe in Tests

    async def go():
        events = ["release", "issue_open", "issue_closed", "pr_open", "pr_closed", "pr_merged"]
        responses["releases"] = (200, [{"id": 1, "name": "v1", "prerelease": False, "draft": False}])
        responses["issues"] = (200, [{"number": 10, "state": "open", "title": "Bug", "labels": [],
                                      "user": {"login": "x"}}])
        responses["pulls"] = (200, [{"number": 20, "state": "open", "merged_at": None, "title": "PR",
                                     "labels": [], "user": {"login": "x"}}])

        # Erster Abruf (baseline): nichts melden, nur Zustand füllen
        notes, state = await provider.poll("o/r", events, {}, {}, baseline=True)
        assert notes == []
        assert state["releases"] == [1] and state["issues"]["10"] == "open"

        # Gleicher Stand, kein baseline mehr: immer noch nichts Neues
        notes, state2 = await provider.poll("o/r", events, {}, state, baseline=False)
        assert notes == []

        # Neuer Release + Issue geschlossen + PR gemerged
        responses["releases"] = (200, [
            {"id": 2, "name": "v2", "prerelease": False, "draft": False},
            {"id": 1, "name": "v1", "prerelease": False, "draft": False},
        ])
        responses["issues"] = (200, [{"number": 10, "state": "closed", "title": "Bug", "labels": [],
                                      "user": {"login": "x"}}])
        responses["pulls"] = (200, [{"number": 20, "state": "closed", "merged_at": "2026-01-01T00:00:00Z",
                                     "title": "PR", "labels": [], "user": {"login": "x"}}])
        notes, state3 = await provider.poll("o/r", events, {}, state2, baseline=False)
        kinds = sorted(n.event for n in notes)
        assert kinds == ["issue_closed", "pr_merged", "release"]
        assert state3["releases"] == [1, 2]
        assert state3["issues"]["10"] == "closed"
        assert state3["prs"]["20"] == "merged"

    run(go())


def test_github_provider_filters_and_baseline_skip():
    from bot.notify.github import GitHubProvider

    provider = GitHubProvider()
    data = [{"id": 1, "name": "v1", "prerelease": True, "draft": False}]

    async def fake_get(path, **params):
        return 200, data

    provider._get = fake_get

    async def go():
        # Baseline registriert die ID, meldet aber nichts
        _, state = await provider.poll("o/r", ["release"], {"prereleases": False}, {}, baseline=True)
        data.append({"id": 2, "name": "v2", "prerelease": True, "draft": False})
        notes, _ = await provider.poll("o/r", ["release"], {"prereleases": False}, state, baseline=False)
        assert notes == []  # Pre-Release durch Filter ausgeschlossen

    run(go())


def test_ticket_panel_custom_ids_roundtrip():
    from bot.cogs.tickets import KINDS, PANEL_PREFIX

    view = __import__("bot.cogs.tickets", fromlist=["TicketPanelView"]).TicketPanelView()
    ids = {item.custom_id for item in view.children}
    assert ids == {f"{PANEL_PREFIX}{k}" for k in KINDS}
    # Jede custom_id muss sich eindeutig auf eine bekannte Ticket-Art zurückführen lassen
    for cid in ids:
        assert cid[len(PANEL_PREFIX):] in KINDS


def test_aion_api_auth_rejects_bad_requests():
    from unittest.mock import AsyncMock

    from aiohttp.test_utils import make_mocked_request

    from bot.cogs.aion_dedup import AionDedup
    from bot.db import Database

    async def go():
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "t.sqlite3")
            await db.connect()
            await db.set(1, "aion_api_token", "secret123")
            await db.set(1, "aion_channel", 999)
            cog = AionDedup.__new__(AionDedup)
            cog.bot = type("B", (), {"get_cog": lambda self, name: None})()
            cog.db = db
            cog.api_pending = {}

            # Kein Authorization-Header -> 401, kein Crash durch das String-Slicing
            req = make_mocked_request("POST", "/api/aion/fight")
            resp = await cog.handle_api_fight(req)
            assert resp.status == 401

            # Falscher Token -> 401
            req = make_mocked_request("POST", "/api/aion/fight", headers={"Authorization": "Bearer falsch"})
            resp = await cog.handle_api_fight(req)
            assert resp.status == 401

            await db.close()

    run(go())


def test_aion_api_merge_and_embed():
    from bot.cogs.aion_dedup import build_run_embed, fmt_dps, fmt_num, merge_players, same_api_run, validate_fight_payload

    assert fmt_num(13_290_000) == "13.29M"
    assert fmt_num(8_080) == "8.1k"
    assert fmt_dps(38_000) == "38.0k/s"

    from bot.cogs.aion_dedup import bar

    assert bar(10, 10, width=10) == "█" * 10
    assert bar(0, 10, width=10) == "░" * 10
    assert bar(5, 10, width=10) == "█" * 5 + "░" * 5
    assert bar(5, 0, width=10) == "░" * 10  # kein Höchstwert -> leer statt Division durch 0

    base = {"dungeon": "Ferocious Horn Den", "bosses_total": 3, "bosses_killed": 3,
            "start_ms": 1_760_445_123_000, "fight_ms": 350_000,
            "players": [{"name": "ReZero", "damage": 13_000_000, "dps": 37_000}]}
    assert validate_fight_payload(base) is None
    assert validate_fight_payload({"dungeon": "X"}) is not None
    assert validate_fight_payload({**base, "players": []}) is not None

    # Zweite Meldung desselben Laufs, leicht abweichende Startzeit/Dauer -> trotzdem derselbe Lauf
    report2 = {**base, "start_ms": base["start_ms"] + 2_000, "fight_ms": base["fight_ms"] - 1_500,
              "players": [{"name": "Gamunex", "damage": 8_000_000, "dps": 23_000}]}
    assert same_api_run(base, report2)

    # 20 Minuten später, sonst identisch -> nicht derselbe Lauf
    later = {**base, "start_ms": base["start_ms"] + 20 * 60_000}
    assert not same_api_run(base, later)

    # Merge: gleicher Spielername in zwei Meldungen -> höherer Schaden gewinnt, sortiert absteigend
    reportA = {**base, "players": [{"name": "ReZero", "damage": 13_000_000, "dps": 37_000},
                                   {"name": "Gamunex", "damage": 8_000_000, "dps": 23_000}]}
    reportB = {**base, "players": [{"name": "ReZero", "damage": 13_290_000, "dps": 38_000},  # höher -> gewinnt
                                   {"name": "Haze", "damage": 5_080_000, "dps": 14_500}]}
    players = merge_players([reportA, reportB])
    assert [p["name"] for p in players] == ["ReZero", "Gamunex", "Haze"]
    assert players[0]["damage"] == 13_290_000  # der höhere der beiden gemeldeten Werte

    embed = build_run_embed(base, players, reporters=2)
    assert "Ferocious Horn Den" in embed.title
    assert "🥇 **ReZero**" in embed.description
    assert "2 DeckBar-Meldung" in embed.footer.text


def test_aion_api_boss_dps():
    from bot.cogs.aion_dedup import build_run_embed, merge_bosses, validate_fight_payload

    base = {"dungeon": "Ferocious Horn Den", "bosses_total": 2, "bosses_killed": 2,
            "start_ms": 1_760_445_123_000, "fight_ms": 350_000,
            "players": [{"name": "ReZero", "damage": 13_000_000, "dps": 37_000}],
            "bosses": [
                {"name": "Boss 1", "players": [{"name": "ReZero", "damage": 4_000_000, "dps": 40_000}]},
                {"name": "Boss 2", "players": [{"name": "ReZero", "damage": 9_000_000, "dps": 35_000}]},
            ]}
    assert validate_fight_payload(base) is None
    assert validate_fight_payload({**base, "bosses": [{"name": "X"}]}) is not None
    assert validate_fight_payload({**base, "bosses": "nope"}) is not None

    reportB = {**base, "bosses": [
        {"name": "Boss 1", "players": [{"name": "Gamunex", "damage": 3_000_000, "dps": 30_000}]},
        {"name": "Boss 2", "players": [{"name": "ReZero", "damage": 9_500_000, "dps": 36_000}]},  # höher -> gewinnt
    ]}
    bosses = merge_bosses([base, reportB])
    assert [b["name"] for b in bosses] == ["Boss 1", "Boss 2"]
    assert {p["name"] for p in bosses[0]["players"]} == {"ReZero", "Gamunex"}
    assert bosses[1]["players"][0]["damage"] == 9_500_000

    embed = build_run_embed(base, base["players"], reporters=2, bosses=bosses)
    field_names = [f.name for f in embed.fields]
    assert "🗡️ Boss 1" in field_names and "🗡️ Boss 2" in field_names


def test_aion_parse_and_match():
    from bot.cogs.aion_dedup import parse_run, same_run

    a = parse_run("**Ferocious Horn Den** – 3/3 Bosse besiegt, Kampfzeit 5:50 (15:42–15:48)")
    b = parse_run("**Ferocious Horn Den** – 3/3 Bosse besiegt, Kampfzeit 5:48 (15:42–15:48)")
    c = parse_run("**Ferocious Horn Den** – 3/3 Bosse besiegt, Kampfzeit 5:51 (15:42–15:48)")
    assert a is not None and a["dungeon"] == "Ferocious Horn Den"
    assert a["kills"] == 3 and a["total"] == 3 and a["duration_s"] == 350
    # drei verschiedene DeckBars, minimal abweichende Werte vom selben Lauf -> alle als gleich erkannt
    assert same_run(a, b) and same_run(a, c) and same_run(b, c)

    # 20 Minuten späterer Re-Run desselben Dungeons -> NICHT zusammenführen
    later = parse_run("**Ferocious Horn Den** – 3/3 Bosse besiegt, Kampfzeit 5:50 (16:02–16:08)")
    assert not same_run(a, later)

    # anderer Dungeon -> nie zusammenführen, auch bei gleicher Zeit
    other_dungeon = parse_run("**Anderer Dungeon** – 3/3 Bosse besiegt, Kampfzeit 5:50 (15:42–15:48)")
    assert not same_run(a, other_dungeon)

    # andere Boss-Anzahl (z. B. ein Boss noch nicht besiegt) -> nicht derselbe abgeschlossene Lauf
    partial = parse_run("**Ferocious Horn Den** – 2/3 Bosse besiegt, Kampfzeit 4:10 (15:42–15:46)")
    assert not same_run(a, partial)

    # Text, der nicht zum DeckBar-Format passt, wird ignoriert statt einen Fehler zu werfen
    assert parse_run("Irgendeine andere Nachricht") is None

    # Einzahl "Boss" bei genau einem Gegner
    single = parse_run("**Solo-Boss** – 1/1 Boss besiegt, Kampfzeit 1:23 (10:00–10:01)")
    assert single is not None and single["total"] == 1


def test_role_can_view():
    import discord

    from bot.cogs.dashboard import role_can_view

    class FakeChannel:
        def __init__(self, overwrites):
            self._ow = overwrites

        def overwrites_for(self, target):
            return self._ow.get(target, discord.PermissionOverwrite())

    class FakeRole:
        def __init__(self, perms):
            self.permissions = perms

    everyone = FakeRole(discord.Permissions(view_channel=True))
    role = FakeRole(discord.Permissions.none())

    # Kein Overwrite: @everyone darf sehen (Basisrecht), also auch diese Rolle
    assert role_can_view(FakeChannel({}), role, everyone) is True

    # @everyone per Overwrite ausgeschlossen, Rolle ohne eigenes Overwrite -> nicht sichtbar
    ch = FakeChannel({everyone: discord.PermissionOverwrite(view_channel=False)})
    assert role_can_view(ch, role, everyone) is False

    # @everyone ausgeschlossen, Rolle aber explizit erlaubt -> rollen-spezifisch gewinnt
    ch = FakeChannel({
        everyone: discord.PermissionOverwrite(view_channel=False),
        role: discord.PermissionOverwrite(view_channel=True),
    })
    assert role_can_view(ch, role, everyone) is True


def test_dashboard_login_redirect():
    import os

    from aiohttp.test_utils import make_mocked_request

    from bot.cogs.dashboard import Dashboard

    cog = Dashboard.__new__(Dashboard)  # kein echter Bot/aiohttp-Server nötig für diesen Handler
    cog._http = None

    async def go():
        old = os.environ.pop("DISCORD_CLIENT_ID", None)
        try:
            resp = await cog.login(make_mocked_request("GET", "/login"))
            assert resp.status == 500  # fehlende Client-ID wird gemeldet, nicht stillschweigend ignoriert

            os.environ["DISCORD_CLIENT_ID"] = "123"
            resp = await cog.login(make_mocked_request("GET", "/login"))
            assert resp.status == 302
            assert "client_id=123" in resp.headers["Location"]
            assert "discord.com/oauth2/authorize" in resp.headers["Location"]
        finally:
            if old is None:
                os.environ.pop("DISCORD_CLIENT_ID", None)
            else:
                os.environ["DISCORD_CLIENT_ID"] = old

    run(go())


def test_dashboard_html_escaping():
    from bot.cogs.dashboard import esc

    assert esc('<script>alert(1)</script>') == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert esc(None) == ""


def test_dps_embed():
    from bot.cogs.dps import build_embed

    assert build_embed({"party": []}) is None
    assert build_embed({}) is None

    e = build_embed({
        "target": "Kromede", "duration_s": 95,
        "party": [
            {"name": "Alice", "cls": "cleric", "dps": 1000, "dmg": 95000, "pct": 20.0},
            {"name": "Bob", "cls": "gladiator", "dps": 5000, "dmg": 475000, "pct": 80.0},
        ],
    })
    assert "Kromede" in e.title
    # Nach DPS absteigend sortiert, trotz Reihenfolge im Payload
    assert e.description.index("Bob") < e.description.index("Alice")
    assert "5.000 DPS" in e.description
    assert "95 s" in e.footer.text


def test_panel_regex():
    import re
    pat = re.compile(r"(?:(<a?:\w+:\d+>|[^\x00-\x7F\s]+)\s*)?<@&(\d+)>")
    got = [(m.group(1), m.group(2)) for m in pat.finditer("🎮 <@&1> und <@&2> <:aion:99> <@&3>")]
    assert got == [("🎮", "1"), (None, "2"), ("<:aion:99>", "3")]


def test_bump_confirmation_detection():
    import discord

    from bot.cogs.bump import DISBOARD_BOT_ID, is_bump_confirmation

    def fake_message(author_id, embeds):
        return type("M", (), {"author": type("A", (), {"id": author_id})(), "embeds": embeds})()

    success = discord.Embed(description="Bump done! :thumbsup: Check it out on disboard.org!")
    cooldown = discord.Embed(description="Please wait another 23 minutes before bumping again.")

    assert is_bump_confirmation(fake_message(DISBOARD_BOT_ID, [success])) is True
    assert is_bump_confirmation(fake_message(DISBOARD_BOT_ID, [cooldown])) is False
    assert is_bump_confirmation(fake_message(123, [success])) is False  # anderer Autor
    assert is_bump_confirmation(fake_message(DISBOARD_BOT_ID, [])) is False  # kein Embed


def test_bump_reminder_sends_role_mention():
    import tempfile
    from pathlib import Path

    from bot.cogs.bump import Bump
    from bot.db import Database

    async def go():
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "t.sqlite3")
            await db.connect()
            await db.set(1, "bump_channel", 42)
            await db.set(1, "bump_role", 7)

            sent = []
            role = type("R", (), {"mention": "<@&7>"})()

            class FakeChannel:
                guild = type("G", (), {"get_role": staticmethod(lambda i: role)})()

                async def send(self, text, **kwargs):
                    sent.append(text)

            cog = Bump.__new__(Bump)
            cog.bot = type("B", (), {"get_channel": lambda self, i: FakeChannel()})()
            cog.db = db

            await cog._remind(1)
            assert sent == ["<@&7> ⏰ Bump ist wieder möglich – wer mag, tippt `/bump`!"]

            await db.close()

    run(go())


def test_stats_channel_names():
    from bot.cogs.stats import bots_channel_name, member_channel_name

    bots = [type("M", (), {"bot": True})(), type("M", (), {"bot": True})(), type("M", (), {"bot": False})()]
    guild = type("G", (), {"member_count": 123, "members": bots})()
    assert member_channel_name(guild) == "👥 Mitglieder: 123"
    assert bots_channel_name(guild) == "🤖 Bots: 2"


def test_new_panel_subviews_build_without_overflow():
    """Discord erlaubt höchstens 5 Komponenten pro Zeile — jede neue Unteransicht des Klick-Panels
    muss ohne ValueError konstruierbar sein, in beiden Verifizierungs-Zuständen."""
    from pathlib import Path

    from bot.cogs.settings import (
        BirthdayView, BumpView, DashboardRoleView, MemberCountView, SettingsPanel, StarboardView, WarnView, VerifyView,
    )
    from bot.db import Database

    cog = type("FakeCog", (), {})()
    cog.db = Database(Path("/tmp/_unused_panel_subview_test.sqlite3"))
    guild = type("G", (), {"id": 1})()
    panel = SettingsPanel.__new__(SettingsPanel)
    panel.cog, panel.guild, panel.persistent = cog, guild, True

    for view_cls in [WarnView, DashboardRoleView, BumpView, MemberCountView, StarboardView, BirthdayView]:
        view = view_cls(panel)
        assert all(item.custom_id for item in view.children)

    for active in (True, False):
        cog.db._settings[1] = {"verify_active": "1" if active else "0"}
        view = VerifyView(panel)
        assert all(item.custom_id for item in view.children)


def test_starboard_embed():
    import datetime

    from bot.cogs.starboard import build_star_embed

    class FakeAuthor:
        id = 1
        display_avatar = type("Avatar", (), {"url": "http://x/avatar.png"})()

        def __str__(self):
            return "Alice#0001"

    class FakeMessage:
        content = "Schaut mal!"
        author = FakeAuthor()
        attachments: list = []
        created_at = datetime.datetime.now(datetime.timezone.utc)
        jump_url = "https://discord.com/channels/1/2/3"

    embed = build_star_embed(FakeMessage(), 5)
    assert embed.description == "Schaut mal!"
    assert embed.author.name == "Alice#0001"
    assert "zur Nachricht" in embed.fields[0].value


def test_starboard_count_excludes_authors_own_star():
    import tempfile
    from pathlib import Path

    from bot.cogs.starboard import STAR_EMOJI, Starboard
    from bot.db import Database

    async def go():
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "t.sqlite3")
            await db.connect()
            cog = Starboard.__new__(Starboard)
            cog.db = db

            class FakeUser:
                def __init__(self, uid):
                    self.id = uid

            class FakeReaction:
                emoji = STAR_EMOJI

                def __init__(self, user_ids):
                    self._ids = user_ids

                async def users(self):
                    for uid in self._ids:
                        yield FakeUser(uid)

            class FakeMessage:
                author = FakeUser(1)
                reactions = [FakeReaction([1, 2, 3])]  # Autor (1) selbst + zwei andere

            count = await cog._star_count(FakeMessage())
            assert count == 2  # der eigene Stern des Autors zählt nicht

            await db.close()

    run(go())


def test_starboard_sync_respects_threshold_and_persists_post():
    import datetime
    import tempfile
    from pathlib import Path

    from bot.cogs.starboard import Starboard
    from bot.db import Database

    async def go():
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "t.sqlite3")
            await db.connect()
            await db.set(1, "starboard_threshold", 3)

            posted = []

            class FakeStarMessage:
                id = 999

            class FakeBoardChannel:
                async def send(self, content=None, embed=None):
                    posted.append((content, embed))
                    return FakeStarMessage()

            class FakeChannel:
                mention = "#general"

            class FakeAuthor:
                id = 1
                display_avatar = type("A", (), {"url": "x"})()

                def __str__(self):
                    return "Alice"

            class FakeGuild:
                id = 1

                def get_channel(self, cid):
                    return FakeBoardChannel()

            class FakeMessage:
                id = 42
                guild = FakeGuild()
                channel = FakeChannel()
                author = FakeAuthor()
                content = "hi"
                attachments: list = []
                created_at = datetime.datetime.now(datetime.timezone.utc)
                jump_url = "https://discord.com/x"

            cog = Starboard.__new__(Starboard)
            cog.db = db

            await cog._sync(FakeMessage(), count=2, board_channel_id=55)
            assert posted == []  # unter der Schwelle -> kein Post

            await cog._sync(FakeMessage(), count=3, board_channel_id=55)
            assert len(posted) == 1  # Schwelle erreicht -> Post
            row = await db.fetchone("SELECT star_message_id FROM starboard WHERE message_id=?", (42,))
            assert row["star_message_id"] == 999

            await db.close()

    run(go())


def test_poll_answer_parsing():
    import pytest as _pytest

    from bot.cogs.polls import parse_answers
    from bot.errors import UserError

    assert parse_answers("Ja | Nein") == ["Ja", "Nein"]
    assert parse_answers(" A | B | C ") == ["A", "B", "C"]

    with _pytest.raises(UserError):
        parse_answers("Nur eine Antwort")  # weniger als 2
    with _pytest.raises(UserError):
        parse_answers("|".join(f"A{i}" for i in range(11)))  # mehr als 10
    with _pytest.raises(UserError):
        parse_answers("x" * 56 + "|Nein")  # einzelne Antwort zu lang


def test_birthday_day_in_month_validation():
    from bot.cogs.birthdays import DAYS_IN_MONTH

    assert DAYS_IN_MONTH[2] == 29  # Februar: 29 erlauben (Schaltjahr), Validierung nicht zu streng
    assert DAYS_IN_MONTH[4] == 30
    assert 31 <= DAYS_IN_MONTH[1]


def test_embed_colour_parsing():
    import discord

    from bot.cogs.embeds import parse_colour
    from bot.errors import UserError

    assert parse_colour(None) == discord.Colour.blurple()
    assert parse_colour("#5865F2") == discord.Colour(0x5865F2)
    assert parse_colour("5865F2") == discord.Colour(0x5865F2)

    with pytest.raises(UserError):
        parse_colour("nicht-hex")


def test_custom_command_rendering_and_name_validation():
    from bot.cogs.customcommands import NAME_RE, render

    member = type("M", (), {"mention": "<@1>"})()
    guild = type("G", (), {"name": "Der Waschsalon"})()
    assert render("Hallo {user}, willkommen auf {server}!", member, guild) == \
        "Hallo <@1>, willkommen auf Der Waschsalon!"

    assert NAME_RE.match("regeln") and NAME_RE.match("info-2")
    assert not NAME_RE.match("Regeln")  # Großbuchstaben nicht erlaubt
    assert not NAME_RE.match("mit leerzeichen")


def test_announcement_due_rows_get_rescheduled():
    import tempfile
    import time
    from pathlib import Path

    from bot.cogs.announcements import Announcements
    from bot.db import Database

    async def go():
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "t.sqlite3")
            await db.connect()
            now = time.time()
            due_id = await db.execute(
                "INSERT INTO scheduled (guild_id, channel_id, text, interval_minutes, next_run) "
                "VALUES (1, 2, 'Hallo!', 30, ?)", (now - 10,),
            )
            not_due_id = await db.execute(
                "INSERT INTO scheduled (guild_id, channel_id, text, interval_minutes, next_run) "
                "VALUES (1, 2, 'Später', 30, ?)", (now + 10_000,),
            )

            sent = []

            class FakeChannel:
                async def send(self, text):
                    sent.append(text)

            cog = Announcements.__new__(Announcements)
            cog.bot = type("B", (), {"get_channel": lambda self, i: FakeChannel()})()
            cog.db = db

            await cog.check_due.coro(cog)  # die echte Loop-Funktion, nicht als Loop (kein wait_until_ready nötig)

            assert sent == ["Hallo!"]  # nur die fällige wurde gepostet
            due_row = await db.fetchone("SELECT next_run FROM scheduled WHERE id=?", (due_id,))
            not_due_row = await db.fetchone("SELECT next_run FROM scheduled WHERE id=?", (not_due_id,))
            assert due_row["next_run"] > now  # neu geplant, 30 Min. weiter in der Zukunft
            assert not_due_row["next_run"] == now + 10_000  # unverändert, war nicht fällig

            await db.close()

    run(go())


def test_voicestats_qualifies_and_format():
    from bot.cogs.voicestats import format_duration, qualifies

    assert format_duration(90) == "1 Min."
    assert format_duration(3661) == "1 Std. 1 Min."

    class FakeVoiceState:
        def __init__(self, self_deaf=False, deaf=False):
            self.self_deaf, self.deaf = self_deaf, deaf

    class FakeMember:
        def __init__(self, bot=False, voice=None):
            self.bot = bot
            self.voice = voice

    class FakeChannel:
        def __init__(self, members, afk=False):
            self.members = members
            self.guild = type("G", (), {"afk_channel": self if afk else None})()

    alice, bob = FakeMember(voice=FakeVoiceState()), FakeMember(voice=FakeVoiceState())
    channel = FakeChannel([alice, bob])
    assert qualifies(alice, channel) is True  # zu zweit, niemand taub -> zählt

    alone_channel = FakeChannel([alice])
    assert qualifies(alice, alone_channel) is False  # allein im Kanal -> zählt nicht

    deaf_bob = FakeMember(voice=FakeVoiceState(self_deaf=True))
    channel2 = FakeChannel([alice, deaf_bob])
    assert qualifies(deaf_bob, channel2) is False  # selbst taub -> zählt nicht
    assert qualifies(alice, channel2) is True  # alice selbst ist nicht taub, zählt weiter

    afk_channel = FakeChannel([alice, bob], afk=True)
    assert qualifies(alice, afk_channel) is False  # AFK-Kanal zählt nie


YOUTUBE_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <yt:videoId>VIDEO1</yt:videoId>
  <title>Erstes Video</title>
  <author><name>Mein Kanal</name></author>
  <media:group><media:thumbnail url="https://i.ytimg.com/vi/VIDEO1/hqdefault.jpg"/></media:group>
 </entry>
 <entry>
  <yt:videoId>VIDEO2</yt:videoId>
  <title>Zweites, neueres Video</title>
  <author><name>Mein Kanal</name></author>
  <media:group><media:thumbnail url="https://i.ytimg.com/vi/VIDEO2/hqdefault.jpg"/></media:group>
 </entry>
</feed>"""


def test_youtube_feed_parsing():
    from bot.notify.youtube import parse_feed

    videos = parse_feed(YOUTUBE_FEED_XML)
    assert [v["id"] for v in videos] == ["VIDEO1", "VIDEO2"]
    assert videos[0]["title"] == "Erstes Video"
    assert videos[0]["author"] == "Mein Kanal"
    assert videos[0]["thumbnail"] == "https://i.ytimg.com/vi/VIDEO1/hqdefault.jpg"


def test_youtube_provider_dedup_and_baseline():
    from bot.notify.youtube import YouTubeProvider

    provider = YouTubeProvider()
    feed_xml = [YOUTUBE_FEED_XML]

    async def fake_fetch(channel_id):
        from bot.notify.youtube import parse_feed
        return parse_feed(feed_xml[0])

    provider._fetch = fake_fetch  # keine echten HTTP-Aufrufe in Tests

    async def go():
        # Baseline: registriert beide Videos, meldet aber nichts
        notes, state = await provider.poll("UCabc", ["video"], {}, {}, baseline=True)
        assert notes == []
        assert set(state["seen"]) == {"VIDEO1", "VIDEO2"}

        # Gleicher Feed, kein baseline mehr -> nichts Neues
        notes2, state2 = await provider.poll("UCabc", ["video"], {}, state, baseline=False)
        assert notes2 == []

        # Ein drittes, neues Video im Feed -> genau eine Meldung
        feed_xml[0] = YOUTUBE_FEED_XML.replace(
            "</feed>", '<entry><yt:videoId>VIDEO3</yt:videoId><title>Drittes</title>'
                       '<author><name>Mein Kanal</name></author></entry></feed>'
        )
        notes3, state3 = await provider.poll("UCabc", ["video"], {}, state2, baseline=False)
        assert len(notes3) == 1 and notes3[0].event == "video"
        assert "VIDEO3" in state3["seen"]

    run(go())


def test_youtube_validate_source_rejects_non_channel_ids():
    from bot.notify.youtube import YouTubeProvider
    from bot.notify.base import ProviderError

    provider = YouTubeProvider()

    async def go():
        with pytest.raises(ProviderError):
            await provider.validate_source("@nicht-die-kanal-id")

    run(go())
