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
            # Reminders.check_due scheitert hier erwartbar an wait_until_ready() (kein echtes
            # Login in diesem Test) — Task sauber abwarten statt "exception was never retrieved".
            task = bot.get_cog("Reminders").check_due.get_task()
            bot.get_cog("Reminders").check_due.cancel()
            if task:
                with contextlib.suppress(Exception):
                    await task
            await bot.get_cog("DpsWebhook").runner.cleanup()
            await bot.db.close()
            await bot.get_cog("Music").session.close()
            return names
    names = run(go())
    for expected in ["play", "radio", "skip", "stop", "warteschlange", "lautstaerke", "wiederholen",
                     "setup log-kanal", "setup temp-voice", "setup panel", "setup management-kanal",
                     "setup verifizierung", "setup verwarnungen", "rollen-panel", "timeout",
                     "warn", "warnungen", "warnung-loeschen", "kick", "ban", "unban",
                     "erinnere", "erinnerungen", "erinnerung-loeschen", "setup dps-webhook",
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
    assert len(view.children) == 7  # kein Schließen-Button im persistenten Modus
    assert all(item.custom_id for item in view.children)

    ephemeral = SettingsPanel(cog, guild=object(), persistent=False)
    assert len(ephemeral.children) == 8  # inkl. Schließen


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
