"""Einstellungen pro Server und Temp-Sprachkanäle in SQLite.

Einstellungen sind einfache Schlüssel/Wert-Paare pro Server. Sie werden beim Start komplett
in den Speicher geladen, weil z. B. der Spamschutz bei jeder Nachricht nachsieht.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    value    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS temp_channels (
    channel_id INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    owner_id   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS levels (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    xp INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
    voice_seconds INTEGER NOT NULL DEFAULT 0, last_xp REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER, user_id INTEGER NOT NULL,
    text TEXT NOT NULL, due REAL NOT NULL, private INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS starboard (
    guild_id INTEGER NOT NULL, message_id INTEGER PRIMARY KEY, star_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS tickets (
    channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    kind TEXT NOT NULL, created REAL NOT NULL, closed REAL
);
CREATE TABLE IF NOT EXISTS birthdays (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day INTEGER NOT NULL, month INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS giveaways (
    message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
    host_id INTEGER NOT NULL, prize TEXT NOT NULL, winners INTEGER NOT NULL, ends REAL NOT NULL,
    ended INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS giveaway_entries (
    message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (message_id, user_id)
);
-- Eigene Befehle, auch im Web-Dashboard bearbeitbar (siehe bot/cogs/dashboard.py, Seite /commands)
CREATE TABLE IF NOT EXISTS custom_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL, response TEXT NOT NULL,
    UNIQUE (guild_id, name)
);
CREATE TABLE IF NOT EXISTS scheduled (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
    text TEXT NOT NULL, interval_minutes INTEGER NOT NULL, next_run REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
    kind TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', last_id TEXT,
    -- events/filters: JSON, state: JSON (zuletzt gesehene IDs je Ereignistyp, für Duplikat-Schutz)
    events TEXT NOT NULL DEFAULT '[]', filters TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE TABLE IF NOT EXISTS level_rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, level INTEGER NOT NULL,
    role_id INTEGER NOT NULL, UNIQUE (guild_id, level)
);
-- AION-2-API-Läufe mit Zeitleiste, für die Analyse-Seite (/runs/<id> im Dashboard, siehe
-- aion_dedup.py). Die eigentlichen (teils mehrere MB großen) Daten liegen als Datei unter
-- <DB_PATH-Ordner>/runs/<id>.json, hier nur der Index zum Aufräumen/Nachschlagen.
CREATE TABLE IF NOT EXISTS aion_runs (
    id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, created REAL NOT NULL,
    dungeon TEXT NOT NULL, difficulty TEXT, reporters INTEGER NOT NULL DEFAULT 1
);
-- Bestenliste je Boss: bester gemeldeter DPS-Wert pro Spieler (über alle Läufe hinweg), für
-- /leaderboard/<guild_id> im Dashboard (siehe aion_dedup.py). run_id verlinkt auf die
-- Analyse-Seite dieses Laufs, falls der eine Zeitleiste hatte (sonst NULL).
CREATE TABLE IF NOT EXISTS aion_leaderboard (
    guild_id INTEGER NOT NULL, boss TEXT NOT NULL, player TEXT NOT NULL,
    class TEXT, damage REAL NOT NULL, dps REAL NOT NULL, run_id TEXT, created REAL NOT NULL,
    PRIMARY KEY (guild_id, boss, player)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None
        self._settings: dict[int, dict[str, str]] = {}

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()
        async with self.conn.execute("SELECT guild_id, key, value FROM settings") as cur:
            async for guild_id, key, value in cur:
                self._settings.setdefault(guild_id, {})[key] = value

    async def _migrate(self) -> None:
        """Spalten, die nach dem ersten `CREATE TABLE IF NOT EXISTS` dazukamen (SQLite legt sie
        bei einer bereits vorhandenen Tabelle nicht automatisch an)."""
        assert self.conn
        async with self.conn.execute("PRAGMA table_info(feeds)") as cur:
            existing = {row[1] async for row in cur}
        for name, ddl in [
            ("events", "ALTER TABLE feeds ADD COLUMN events TEXT NOT NULL DEFAULT '[]'"),
            ("filters", "ALTER TABLE feeds ADD COLUMN filters TEXT NOT NULL DEFAULT '{}'"),
            ("enabled", "ALTER TABLE feeds ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"),
            ("state", "ALTER TABLE feeds ADD COLUMN state TEXT NOT NULL DEFAULT '{}'"),
            ("error", "ALTER TABLE feeds ADD COLUMN error TEXT"),
        ]:
            if name not in existing:
                await self.conn.execute(ddl)

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---- Allgemeine Abfragen --------------------------------------------------------------

    async def execute(self, sql: str, params: tuple = ()) -> int:
        """Führt aus und speichert. Gibt lastrowid zurück."""
        assert self.conn
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid

    async def fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        assert self.conn
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        assert self.conn
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    def get_ids(self, guild_id: int, key: str) -> list[int]:
        """Kommagetrennte ID-Liste (z. B. Kanäle ohne XP)."""
        return [int(x) for x in (self.get(guild_id, key) or "").split(",") if x.strip().isdigit()]

    # ---- Einstellungen -------------------------------------------------------------------

    def get(self, guild_id: int, key: str, default: str | None = None) -> str | None:
        return self._settings.get(guild_id, {}).get(key, default)

    def get_int(self, guild_id: int, key: str) -> int | None:
        value = self.get(guild_id, key)
        return int(value) if value and value.isdigit() else None

    def get_bool(self, guild_id: int, key: str, default: bool = False) -> bool:
        value = self.get(guild_id, key)
        return default if value is None else value == "1"

    def all(self, guild_id: int) -> dict[str, str]:
        return dict(self._settings.get(guild_id, {}))

    def guilds_with(self, key: str) -> list[int]:
        """Server-IDs, bei denen `key` gesetzt ist (z. B. für Neustart-Registrierung persistenter Views)."""
        return [gid for gid, kv in self._settings.items() if key in kv]

    def guild_for_value(self, key: str, value: str) -> int | None:
        """Server-ID, bei der `key` genau `value` ist (z. B. Token → Server beim DPS-Webhook)."""
        for gid in self.guilds_with(key):
            if self.get(gid, key) == value:
                return gid
        return None

    async def set(self, guild_id: int, key: str, value: str | int | bool | None) -> None:
        """Setzt einen Wert, `None` löscht ihn."""
        assert self.conn
        if value is None:
            self._settings.get(guild_id, {}).pop(key, None)
            await self.conn.execute("DELETE FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
        else:
            if isinstance(value, bool):
                value = "1" if value else "0"
            value = str(value)
            self._settings.setdefault(guild_id, {})[key] = value
            await self.conn.execute(
                "INSERT INTO settings (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                (guild_id, key, value),
            )
        await self.conn.commit()

    # ---- Temp-Sprachkanäle ---------------------------------------------------------------

    async def add_temp_channel(self, channel_id: int, guild_id: int, owner_id: int) -> None:
        assert self.conn
        await self.conn.execute(
            "INSERT OR REPLACE INTO temp_channels VALUES (?, ?, ?)", (channel_id, guild_id, owner_id)
        )
        await self.conn.commit()

    async def remove_temp_channel(self, channel_id: int) -> None:
        assert self.conn
        await self.conn.execute("DELETE FROM temp_channels WHERE channel_id=?", (channel_id,))
        await self.conn.commit()

    async def temp_channel_owner(self, channel_id: int) -> int | None:
        assert self.conn
        async with self.conn.execute(
            "SELECT owner_id FROM temp_channels WHERE channel_id=?", (channel_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_temp_channel_owner(self, channel_id: int, owner_id: int) -> None:
        assert self.conn
        await self.conn.execute(
            "UPDATE temp_channels SET owner_id=? WHERE channel_id=?", (owner_id, channel_id)
        )
        await self.conn.commit()

    async def temp_channels(self) -> list[tuple[int, int, int]]:
        assert self.conn
        async with self.conn.execute("SELECT channel_id, guild_id, owner_id FROM temp_channels") as cur:
            return [tuple(row) async for row in cur]
