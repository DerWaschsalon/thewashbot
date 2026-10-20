"""AION-2: mehrere DeckBar-Posts vom selben Dungeon-Lauf zu einem zusammenfassen.

Zwei unabhängige Wege, je nachdem ob DeckBar selbst geändert wurde:

1. **Nachrichten-Filter** (kein DeckBar-Eingriff nötig): DeckBar postet weiterhin unverändert
   über seinen eigenen Discord-Webhook (github.com/gamunex/deckbar,
   `daemon/deckbar_daemon/runs.py`, `run_message`/`post_discord`). Dieser Cog beobachtet den per
   `/aion kanal:` festgelegten Kanal, erkennt mehrere Nachrichten vom selben Lauf (Dungeon,
   Boss-Anzahl, ähnliche Dauer/Startzeit aus dem Text) und löscht alle außer der zuerst
   angekommenen. Format-Beispiel:
       **Ferocious Horn Den** – 3/3 Bosse besiegt, Kampfzeit 5:50 (15:42–15:48)
   Spielernamen stehen hier nur im angehängten Bild, nicht im Text — kein Namens-Filter möglich.

2. **API** (braucht eine Änderung an DeckBar, die an gamunex geht): `POST /api/aion/fight` auf
   diesem Server (Port 8789, siehe `bot/cogs/dashboard.py`) nimmt strukturierte Lauf-Daten mit
   echten Spielernamen/Schaden entgegen, merged mehrere Meldungen desselben Laufs zu einer
   Rangliste und postet genau einmal ein eigenes Embed. Schutz über einen gemeinsamen Token
   (`/aion token`), da der Endpunkt über Cloudflare öffentlich erreichbar ist.

   Erwarteter Payload (ein POST pro abgeschlossenem Lauf, eine Zeile pro Spieler):
       {
         "dungeon": "Ferocious Horn Den", "difficulty": "Normal",
         "bosses_total": 3, "bosses_killed": 3,
         "start_ms": 1760445123000, "fight_ms": 350000,
         "players": [{"name": "ReZero", "damage": 13290000, "dps": 38000}, …],
         "bosses": [                                          # optional, für DPS pro Boss
           {"name": "Boss 1", "players": [{"name": "ReZero", "damage": 4200000, "dps": 41000}, …]},
           …
         ],
         "timeline": [                                        # optional, für die Analyse-Seite
           {"t_ms": 1234, "player": "ReZero", "skill": "Arrow of Reckoning",
            "damage": 41200, "crit": true, "back": false, "perfect": false, "double": false},
           …
         ],
         "players_meta": [                                    # optional, Klasse/Ausrüstung
           {"name": "ReZero", "class": "Ranger", "gear_score": 2850, "combat_power": 12500}, …
         ],
         "boss_hp": [                                          # optional, für die HP-Kurve
           {"t_ms": 0, "hp_pct": 100.0}, {"t_ms": 5000, "hp_pct": 92.3}, …
         ],
         "heals": [                                            # optional, Heilungs-Übersicht
           {"t_ms": 1234, "player": "Priest1", "target": "ReZero", "amount": 8200}, …
         ]
       }

   Mit `timeline` postet der Bot zusätzlich einen Link auf eine eigene Analyse-Seite
   (`/runs/<id>` im Web-Dashboard, siehe `bot/cogs/dashboard.py`, Optik/Umfang an AbyssLogs
   angelehnt) mit Skill-Aufschlüsselung pro Spieler, DPS-über-Zeit-Diagramm, Trefferstatistik
   (Crit-/Back-Attack-/Perfect-/Double-Rate), und — soweit mitgeschickt — Klasse/Ausrüstung,
   Boss-HP-Kurve (Berserk-Zeitpunkt) und Heilungsübersicht. Ohne `timeline` nur die Rangliste wie
   bisher. `player` muss zu einem Namen in `players[]` passen; `skill`/`crit`/`back`/`perfect`/
   `double` sowie die drei weiteren Felder sind alle optional und unabhängig voneinander nutzbar.
   `skill` idealerweise schon als lesbarer Name (wie `boss_name_en`/`dungeon_parts_en` bei den
   anderen Feldern), nicht als Zahlencode — ohne eigene Skill-Datenbank kann der Bot Codes nicht
   auflösen. Kommen mehrere Meldungen für denselben Lauf (mehrere Mitspieler), wird jeweils die
   Liste mit den meisten Einträgen genommen, nicht mehrere unabhängige Mitschnitte
   event-genau zusammengeführt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

RUN_RE = re.compile(
    r"^\*\*(?P<dungeon>.+?)\*\*\s+–\s+(?P<kills>\d+)/(?P<total>\d+)\s+(?:Boss|Bosse)\s+besiegt,\s+"
    r"Kampfzeit\s+(?P<dmin>\d+):(?P<dsec>\d{2})\s+\((?P<start>\d{1,2}:\d{2})–(?P<end>\d{1,2}:\d{2})\)"
)
DEFAULT_DELAY = 15
DURATION_TOLERANCE_S = 20
START_TOLERANCE_MIN = 1
API_START_TOLERANCE_MS = 30_000
MEDALS = ["🥇", "🥈", "🥉"]


def fmt_num(n: float) -> str:
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:.0f}"


def fmt_dps(n: float) -> str:
    return f"{fmt_num(n)}/s"


def bar(value: float, max_value: float, width: int = 10) -> str:
    """Balkengrafik relativ zum besten Wert in der Gruppe, à la Details!/Raider.io-DPS-Meter."""
    if max_value <= 0:
        return "░" * width
    filled = max(0, min(width, round(width * value / max_value)))
    return "█" * filled + "░" * (width - filled)


# ---- Variante 1: Text-Nachrichten im bestehenden DeckBar-Webhook-Kanal filtern --------------


def parse_run(content: str) -> dict | None:
    m = RUN_RE.match(content.strip())
    if not m:
        return None
    hh, mm = m["start"].split(":")
    start_min = int(hh) * 60 + int(mm)
    return {
        "dungeon": m["dungeon"], "kills": int(m["kills"]), "total": int(m["total"]),
        "duration_s": int(m["dmin"]) * 60 + int(m["dsec"]), "start_min": start_min,
    }


def same_run(a: dict, b: dict) -> bool:
    if a["dungeon"] != b["dungeon"] or a["kills"] != b["kills"] or a["total"] != b["total"]:
        return False
    if abs(a["duration_s"] - b["duration_s"]) > DURATION_TOLERANCE_S:
        return False
    # Minuten-Rollover (z. B. 23:59 vs. 00:01) über den Tagesrand ignorieren wir bewusst — ein
    # Dungeon-Lauf, der genau um Mitternacht beginnt, ist selten genug, das in Kauf zu nehmen.
    if abs(a["start_min"] - b["start_min"]) > START_TOLERANCE_MIN:
        return False
    return True


class Cluster:
    def __init__(self, run: dict, keeper: discord.Message):
        self.run = run
        self.keeper = keeper
        self.duplicates: list[discord.Message] = []
        self.task: asyncio.Task | None = None


# ---- Variante 2: strukturierte API --------------------------------------------------------


def validate_fight_payload(data: dict) -> str | None:
    """Gibt eine Fehlermeldung zurück, oder None wenn der Payload brauchbar ist."""
    if not isinstance(data.get("dungeon"), str) or not data["dungeon"].strip():
        return "dungeon fehlt"
    for key in ("bosses_total", "bosses_killed", "start_ms", "fight_ms"):
        if not isinstance(data.get(key), (int, float)):
            return f"{key} fehlt oder ist keine Zahl"
    players = data.get("players")
    if not isinstance(players, list) or not players:
        return "players fehlt oder ist leer"
    for p in players:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
            return "players[].name fehlt"
        if not isinstance(p.get("damage"), (int, float)):
            return "players[].damage fehlt oder ist keine Zahl"
    bosses = data.get("bosses")
    if bosses is not None:
        if not isinstance(bosses, list):
            return "bosses muss eine Liste sein"
        for b in bosses:
            if not isinstance(b, dict) or not isinstance(b.get("name"), str) or not b["name"].strip():
                return "bosses[].name fehlt"
            boss_players = b.get("players")
            if not isinstance(boss_players, list) or not boss_players:
                return "bosses[].players fehlt oder ist leer"
            for p in boss_players:
                if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
                    return "bosses[].players[].name fehlt"
                if not isinstance(p.get("damage"), (int, float)):
                    return "bosses[].players[].damage fehlt oder ist keine Zahl"
    timeline = data.get("timeline")
    if timeline is not None:
        if not isinstance(timeline, list):
            return "timeline muss eine Liste sein"
        for ev in timeline:
            if not isinstance(ev, dict) or not isinstance(ev.get("t_ms"), (int, float)):
                return "timeline[].t_ms fehlt oder ist keine Zahl"
            if not isinstance(ev.get("player"), str) or not ev["player"].strip():
                return "timeline[].player fehlt"
            if not isinstance(ev.get("damage"), (int, float)):
                return "timeline[].damage fehlt oder ist keine Zahl"
            buffs = ev.get("buffs")
            if buffs is not None and not isinstance(buffs, list):
                return "timeline[].buffs muss eine Liste sein"
    players_meta = data.get("players_meta")
    if players_meta is not None:
        if not isinstance(players_meta, list):
            return "players_meta muss eine Liste sein"
        for pm in players_meta:
            if not isinstance(pm, dict) or not isinstance(pm.get("name"), str) or not pm["name"].strip():
                return "players_meta[].name fehlt"
    boss_hp = data.get("boss_hp")
    if boss_hp is not None:
        if not isinstance(boss_hp, list):
            return "boss_hp muss eine Liste sein"
        for s in boss_hp:
            if not isinstance(s, dict) or not isinstance(s.get("t_ms"), (int, float)):
                return "boss_hp[].t_ms fehlt oder ist keine Zahl"
            if not isinstance(s.get("hp_pct"), (int, float)):
                return "boss_hp[].hp_pct fehlt oder ist keine Zahl"
    heals = data.get("heals")
    if heals is not None:
        if not isinstance(heals, list):
            return "heals muss eine Liste sein"
        for h in heals:
            if not isinstance(h, dict) or not isinstance(h.get("t_ms"), (int, float)):
                return "heals[].t_ms fehlt oder ist keine Zahl"
            if not isinstance(h.get("player"), str) or not h["player"].strip():
                return "heals[].player fehlt"
            if not isinstance(h.get("amount"), (int, float)):
                return "heals[].amount fehlt oder ist keine Zahl"
    skills = data.get("skills")
    if skills is not None:
        if not isinstance(skills, dict):
            return "skills muss ein Objekt sein (Skill-Schlüssel -> {name, icon})"
        for meta in skills.values():
            if not isinstance(meta, dict):
                return "skills[].* muss ein Objekt sein"
    attempts = data.get("attempts")
    if attempts is not None:
        if not isinstance(attempts, list):
            return "attempts muss eine Liste sein"
        for a in attempts:
            if not isinstance(a, dict) or not isinstance(a.get("label"), str) or not a["label"].strip():
                return "attempts[].label fehlt"
            if not isinstance(a.get("players"), list) or not a["players"]:
                return "attempts[].players fehlt oder ist leer"
            for p in a["players"]:
                if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
                    return "attempts[].players[].name fehlt"
                if not isinstance(p.get("damage"), (int, float)):
                    return "attempts[].players[].damage fehlt oder ist keine Zahl"
    return None


def same_api_run(a: dict, b: dict) -> bool:
    if a["dungeon"] != b["dungeon"] or a["bosses_total"] != b["bosses_total"] or a["bosses_killed"] != b["bosses_killed"]:
        return False
    if abs(a["fight_ms"] - b["fight_ms"]) > DURATION_TOLERANCE_S * 1000:
        return False
    if abs(a["start_ms"] - b["start_ms"]) > API_START_TOLERANCE_MS:
        return False
    return True


def merge_players(reports: list[dict]) -> list[dict]:
    """Ein Eintrag pro Spielername, über alle Meldungen desselben Laufs — bei Abweichungen
    (unterschiedliche lokale Mitschnitte) gewinnt der höhere Schadenswert."""
    best: dict[str, dict] = {}
    for report in reports:
        for p in report["players"]:
            name = p["name"].strip()
            cur = best.get(name)
            if cur is None or p["damage"] > cur["damage"]:
                best[name] = {"name": name, "damage": p["damage"], "dps": p.get("dps") or 0}
    return sorted(best.values(), key=lambda p: -p["damage"])


def merge_bosses(reports: list[dict]) -> list[dict]:
    """Pro Boss (Reihenfolge nach erster Meldung) die Spieler mergen wie bei merge_players."""
    order: list[str] = []
    per_boss: dict[str, list[dict]] = {}
    for report in reports:
        for b in report.get("bosses") or []:
            name = b["name"].strip()
            if name not in per_boss:
                per_boss[name] = []
                order.append(name)
            per_boss[name].append({"players": b["players"]})
    return [{"name": name, "players": merge_players(per_boss[name])} for name in order]


def pick_best_list(reports: list[dict], key: str) -> list[dict] | None:
    """Für `timeline`/`players_meta`/`boss_hp`/`heals`: jede Meldung sieht (je nach
    deckbar-capture) im Prinzip dieselben Rohdaten der ganzen Gruppe — nimmt die Liste mit den
    meisten Einträgen, statt zu versuchen, mehrere unabhängige Mitschnitte event-genau
    zusammenzuführen."""
    best: list[dict] | None = None
    for report in reports:
        values = report.get(key)
        if isinstance(values, list) and (best is None or len(values) > len(best)):
            best = values
    return best


def pick_best_timeline(reports: list[dict]) -> list[dict] | None:
    return pick_best_list(reports, "timeline")


def bucket_dps(timeline: list[dict], fight_ms: int, buckets: int = 60) -> dict:
    """Schaden pro Spieler in `buckets` gleich großen Zeitfenstern über die Kampfdauer, fürs
    DPS-über-Zeit-Diagramm auf der Analyse-Seite (`/runs/<id>` im Dashboard)."""
    buckets = max(1, buckets)
    bucket_ms = max(1.0, fight_ms / buckets)
    labels_s = [round((i + 1) * bucket_ms / 1000, 1) for i in range(buckets)]
    totals: dict[str, list[float]] = {}
    for ev in timeline:
        name = ev.get("player")
        if not name:
            continue
        idx = min(buckets - 1, max(0, int(ev.get("t_ms", 0) // bucket_ms)))
        totals.setdefault(name, [0.0] * buckets)[idx] += float(ev.get("damage") or 0)
    bucket_s = bucket_ms / 1000
    series = {name: [round(v / bucket_s, 1) for v in values] for name, values in totals.items()}
    return {"labels_s": labels_s, "series": series}


def boss_hp_series(boss_hp: list[dict]) -> dict:
    """`boss_hp`-Samples fürs HP-über-Zeit-Diagramm (Berserk-/Enrage-Zeitpunkt erkennbar an
    einem Sprung in der Abnahme-Geschwindigkeit)."""
    ordered = sorted(boss_hp, key=lambda s: s.get("t_ms", 0))
    return {
        "labels_s": [round(s.get("t_ms", 0) / 1000, 1) for s in ordered],
        "values": [round(float(s.get("hp_pct") or 0), 1) for s in ordered],
    }


def heals_summary(heals: list[dict]) -> list[dict]:
    """Pro Heiler: Gesamtheilung und Anzahl Heilungen, absteigend — für die Heilungsübersicht."""
    per_player: dict[str, dict] = {}
    for h in heals:
        name = h.get("player")
        if not name:
            continue
        s = per_player.setdefault(name, {"name": name, "heals": 0, "amount": 0.0})
        s["heals"] += 1
        s["amount"] += float(h.get("amount") or 0)
    return sorted(per_player.values(), key=lambda s: -s["amount"])


def timeline_stats(timeline: list[dict]) -> dict[str, dict]:
    """Pro Spieler: Trefferzahl, Crit-/Back-Attack-/Perfect-/Double-Rate, Gesamtschaden aus der
    Zeitleiste — unabhängig von den schon gemeldeten players[]-Summen, direkt aus den
    Einzelereignissen."""
    stats: dict[str, dict] = {}
    for ev in timeline:
        name = ev.get("player")
        if not name:
            continue
        s = stats.setdefault(name, {"hits": 0, "crits": 0, "backs": 0, "perfects": 0, "doubles": 0, "damage": 0.0})
        s["hits"] += 1
        s["crits"] += 1 if ev.get("crit") else 0
        s["backs"] += 1 if ev.get("back") else 0
        s["perfects"] += 1 if ev.get("perfect") else 0
        s["doubles"] += 1 if ev.get("double") else 0
        s["damage"] += float(ev.get("damage") or 0)
    for s in stats.values():
        for flag, key in (("crit", "crits"), ("back", "backs"), ("perfect", "perfects"), ("double", "doubles")):
            s[f"{flag}_pct"] = round(100 * s[key] / s["hits"], 1) if s["hits"] else 0.0
    return stats


def skill_breakdown(timeline: list[dict], skills: dict[str, dict] | None = None, top: int = 8) -> dict[str, list[dict]]:
    """Pro Spieler die Top-`top`-Skills nach Gesamtschaden (Name, Icon, Treffer, Schaden, Anteil
    an dessen Gesamtschaden) — die Skill-Aufschlüsselung, die auf der Analyse-Seite angezeigt
    wird. `skills` löst den rohen `timeline[].skill`-Schlüssel (Code oder schon ein Name) in Name
    + Icon-URL auf (AbyssLogs-Stil: eine Lookup-Tabelle statt Name/Icon pro Einzelereignis) —
    fehlt ein Schlüssel darin, wird der rohe Wert selbst als Name angezeigt."""
    skills = skills or {}
    per_player: dict[str, dict[str, dict]] = {}
    for ev in timeline:
        name = ev.get("player")
        if not name:
            continue
        key = str(ev.get("skill") or "–")
        bucket = per_player.setdefault(name, {})
        s = bucket.setdefault(key, {"key": key, "hits": 0, "damage": 0.0})
        s["hits"] += 1
        s["damage"] += float(ev.get("damage") or 0)
    result: dict[str, list[dict]] = {}
    for name, bucket in per_player.items():
        total = sum(s["damage"] for s in bucket.values()) or 1.0
        ordered = sorted(bucket.values(), key=lambda s: -s["damage"])[:top]
        for s in ordered:
            s["pct"] = round(100 * s["damage"] / total, 1)
            meta = skills.get(s["key"]) or {}
            s["skill"] = meta.get("name") or s["key"]
            s["icon"] = meta.get("icon")
        result[name] = ordered
    return result


def buff_uptime(timeline: list[dict], skills: dict[str, dict] | None = None, top: int = 10) -> dict[str, list[dict]]:
    """Pro Spieler die Top-`top`-Buffs/Debuffs nach Anwesenheits-Rate: an wie viel % der eigenen
    Treffer dieser Buff aktiv war (`timeline[].buffs`, optionale Liste von Skill-Codes/-Namen je
    Treffer — anders als `buffSpans` bei AbyssLogs keine durchgehende Zeitspanne, sondern nur die
    Stichprobe „war es bei diesem Treffer aktiv“, daher eine Rate statt einer echten Uptime-Zeit).
    `skills` löst Codes in Name + Icon auf, wie bei `skill_breakdown`."""
    skills = skills or {}
    hits_per_player: dict[str, int] = {}
    presence: dict[str, dict[str, int]] = {}
    for ev in timeline:
        name = ev.get("player")
        if not name:
            continue
        hits_per_player[name] = hits_per_player.get(name, 0) + 1
        for buff_key in ev.get("buffs") or []:
            bucket = presence.setdefault(name, {})
            bucket[buff_key] = bucket.get(buff_key, 0) + 1
    result: dict[str, list[dict]] = {}
    for name, buffs in presence.items():
        hits = hits_per_player.get(name) or 1
        ordered = sorted(buffs.items(), key=lambda kv: -kv[1])[:top]
        rows = []
        for key, count in ordered:
            meta = skills.get(key) or {}
            rows.append({
                "key": key, "buff": meta.get("name") or key, "icon": meta.get("icon"),
                "pct": round(100 * count / hits, 1),
            })
        result[name] = rows
    return result


def leaderboard_rows(bosses: list[dict], players_meta: list[dict] | None) -> list[dict]:
    """Pro Boss und Spieler eine Zeile (Schaden, DPS, Klasse falls bekannt) — Kandidaten fürs
    Upsert in `aion_leaderboard` (dort gewinnt dann nur, wer den bisherigen Bestwert übertrifft)."""
    class_by_name = {m["name"]: m.get("class") for m in (players_meta or [])}
    rows = []
    for b in bosses:
        for p in b.get("players") or []:
            rows.append({
                "boss": b["name"], "player": p["name"], "class": class_by_name.get(p["name"]),
                "damage": p["damage"], "dps": p.get("dps") or 0,
            })
    return rows


def merge_skill_maps(reports: list[dict]) -> dict[str, dict]:
    """Vereint die `skills`-Lookup-Tabellen aller Meldungen (anders als bei den Listen-Feldern
    sinnvoll zu vereinen statt nur die größte zu nehmen — unterschiedliche Mitschnitte können
    unterschiedliche Skills aufgelöst haben)."""
    merged: dict[str, dict] = {}
    for report in reports:
        s = report.get("skills")
        if isinstance(s, dict):
            merged.update(s)
    return merged


def runs_dir(db) -> Path:
    d = db.path.parent / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_run_embed(
    run: dict, players: list[dict], reporters: int, bosses: list[dict] | None = None, run_id: str | None = None,
    guild_id: int | None = None,
) -> discord.Embed:
    title = f"⚔️ {run['dungeon']}" + (f" ({run['difficulty']})" if run.get("difficulty") else "")
    max_damage = players[0]["damage"] if players else 0
    lines = []
    for i, p in enumerate(players[:15]):
        rank = MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
        lines.append(
            f"{rank} **{p['name']}**  `{bar(p['damage'], max_damage)}`  {fmt_num(p['damage'])}  {fmt_dps(p['dps'])}"
        )
    minutes, seconds = divmod(round(run["fight_ms"] / 1000), 60)
    e = discord.Embed(title=title, colour=discord.Colour.gold(), description="\n".join(lines))
    e.add_field(name="Bosse", value=f"{run['bosses_killed']}/{run['bosses_total']} besiegt")
    e.add_field(name="Kampfzeit", value=f"{minutes}:{seconds:02d}")
    for b in (bosses or [])[:20]:
        boss_players = b["players"][:10]
        max_dps = max((p["dps"] for p in boss_players), default=0)
        boss_lines = [
            f"**{p['name']}**  `{bar(p['dps'], max_dps, width=8)}`  {fmt_dps(p['dps'])}" for p in boss_players
        ]
        e.add_field(name=f"🗡️ {b['name']}", value="\n".join(boss_lines) or "–", inline=False)
    if run_id or guild_id:
        from .dashboard import dashboard_base_url

        base = dashboard_base_url()
        links = []
        if run_id:
            links.append(f"[Zeitleiste ansehen]({base}/runs/{run_id})")
        if guild_id and bosses:
            links.append(f"[Bestenliste]({base}/leaderboard/{guild_id})")
        e.add_field(name="📊 Analyse", value=" · ".join(links), inline=False)
    e.set_footer(text=f"AION-2 · {reporters} DeckBar-Meldung(en) zusammengeführt")
    return e


class ApiCluster:
    def __init__(self, run: dict, guild_id: int):
        self.run = run
        self.guild_id = guild_id
        self.reports: list[dict] = [run]
        self.task: asyncio.Task | None = None


RUN_RETENTION_DAYS = 30


class AionDedup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.pending: dict[int, list[Cluster]] = {}  # guild_id -> offene Text-Cluster
        self.api_pending: dict[int, list[ApiCluster]] = {}  # guild_id -> offene API-Cluster
        self.cleanup_runs.start()

    def cog_unload(self):
        self.cleanup_runs.cancel()
        for clusters in self.pending.values():
            for c in clusters:
                if c.task:
                    c.task.cancel()
        for clusters in self.api_pending.values():
            for c in clusters:
                if c.task:
                    c.task.cancel()

    @tasks.loop(hours=24)
    async def cleanup_runs(self):
        cutoff = time.time() - RUN_RETENTION_DAYS * 86400
        rows = await self.db.fetchall("SELECT id FROM aion_runs WHERE created < ?", (cutoff,))
        for r in rows:
            (runs_dir(self.db) / f"{r['id']}.json").unlink(missing_ok=True)
        if rows:
            await self.db.execute("DELETE FROM aion_runs WHERE created < ?", (cutoff,))
            log.info("AION-Analyse: %d alte Läufe (älter als %d Tage) aufgeräumt", len(rows), RUN_RETENTION_DAYS)

    @cleanup_runs.before_loop
    async def before_cleanup_runs(self):
        await self.bot.wait_until_ready()

    # ---- Variante 1: Nachrichten im DeckBar-Kanal -----------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.webhook_id is None or not message.guild:
            return
        channel_id = self.db.get_int(message.guild.id, "aion_channel")
        if not channel_id or message.channel.id != channel_id:
            return
        if (message.author.name or "").strip().lower() != "deckbar":
            return
        run = parse_run(message.content or "")
        if run is None:
            return

        clusters = self.pending.setdefault(message.guild.id, [])
        for c in clusters:
            if same_run(c.run, run):
                c.duplicates.append(message)
                log.info("AION: %s als Duplikat von %s erkannt (%s)", message.id, c.keeper.id, run["dungeon"])
                return

        delay = self.db.get_int(message.guild.id, "aion_dedup_delay") or DEFAULT_DELAY
        cluster = Cluster(run, message)
        clusters.append(cluster)
        cluster.task = asyncio.create_task(self._finalize_later(message.guild.id, cluster, delay))

    async def _finalize_later(self, guild_id: int, cluster: Cluster, delay: int) -> None:
        await asyncio.sleep(delay)
        clusters = self.pending.get(guild_id, [])
        if cluster in clusters:
            clusters.remove(cluster)
        if not cluster.duplicates:
            return
        log.info(
            "AION: %s – behalte %s, lösche %d Duplikat(e)",
            cluster.run["dungeon"], cluster.keeper.id, len(cluster.duplicates),
        )
        for dup in cluster.duplicates:
            try:
                await dup.delete()
            except discord.HTTPException as e:
                log.warning("AION: Duplikat %s konnte nicht gelöscht werden: %r", dup.id, e)

    # ---- Variante 2: API -------------------------------------------------------------------

    def api_url(self) -> str:
        # öffentliche Adresse (z. B. über Cloudflare Tunnel), NICHT die LAN-IP — die Gegenseite
        # (DeckBar bei anderen Spielern) sitzt oft außerhalb des eigenen Netzes.
        base = os.getenv("AION_PUBLIC_URL", "").rstrip("/") or "<AION_PUBLIC_URL fehlt in .env>"
        return f"{base}/api/aion/fight"

    async def handle_api_fight(self, request: web.Request) -> web.Response:
        guild_id = self.db.guild_for_value("aion_api_token", (request.headers.get("Authorization") or "")[7:])
        if guild_id is None or not (request.headers.get("Authorization") or "").startswith("Bearer "):
            return web.json_response({"error": "unauthorized"}, status=401)
        channel_id = self.db.get_int(guild_id, "aion_channel")
        if not channel_id:
            return web.json_response({"error": "kein aion_channel eingerichtet (/aion kanal:)"}, status=409)
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "invalid json"}, status=400)
        error = validate_fight_payload(data)
        if error:
            return web.json_response({"error": error}, status=400)

        run = {
            "dungeon": data["dungeon"].strip(), "difficulty": (data.get("difficulty") or "").strip() or None,
            "bosses_total": int(data["bosses_total"]), "bosses_killed": int(data["bosses_killed"]),
            "start_ms": int(data["start_ms"]), "fight_ms": int(data["fight_ms"]), "players": data["players"],
            "bosses": data.get("bosses") or [], "timeline": data.get("timeline"),
            "players_meta": data.get("players_meta"), "boss_hp": data.get("boss_hp"), "heals": data.get("heals"),
            "skills": data.get("skills"), "attempts": data.get("attempts"),
        }
        clusters = self.api_pending.setdefault(guild_id, [])
        for c in clusters:
            if same_api_run(c.run, run):
                c.reports.append(run)
                log.info("AION-API: Lauf %s bekommt weitere Meldung (jetzt %d)", run["dungeon"], len(c.reports))
                return web.json_response({"ok": True, "merged": True})

        delay = self.db.get_int(guild_id, "aion_dedup_delay") or DEFAULT_DELAY
        cluster = ApiCluster(run, guild_id)
        clusters.append(cluster)
        cluster.task = asyncio.create_task(self._finalize_api_later(cluster, delay, channel_id))
        return web.json_response({"ok": True, "merged": False})

    async def _finalize_api_later(self, cluster: ApiCluster, delay: int, channel_id: int) -> None:
        await asyncio.sleep(delay)
        clusters = self.api_pending.get(cluster.guild_id, [])
        if cluster in clusters:
            clusters.remove(cluster)
        channel = self.bot.get_channel(channel_id)
        if not channel:
            log.warning("AION-API: Zielkanal %s nicht gefunden", channel_id)
            return
        players = merge_players(cluster.reports)
        bosses = merge_bosses(cluster.reports)
        players_meta = pick_best_list(cluster.reports, "players_meta")
        timeline = pick_best_list(cluster.reports, "timeline")
        attempts = pick_best_list(cluster.reports, "attempts")
        extras = {
            "players_meta": players_meta,
            "boss_hp": pick_best_list(cluster.reports, "boss_hp"),
            "heals": pick_best_list(cluster.reports, "heals"),
            "skills": merge_skill_maps(cluster.reports),
            "attempts": attempts,
        }
        run_id = await self._store_run(cluster.guild_id, cluster.run, players, bosses, timeline, extras, len(cluster.reports)) \
            if (timeline or attempts) else None
        if bosses:
            await self._update_leaderboard(cluster.guild_id, bosses, players_meta, run_id)
        embed = build_run_embed(cluster.run, players, len(cluster.reports), bosses, run_id=run_id, guild_id=cluster.guild_id)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.warning("AION-API: Posten fehlgeschlagen: %r", e)

    async def _update_leaderboard(
        self, guild_id: int, bosses: list[dict], players_meta: list[dict] | None, run_id: str | None,
    ) -> None:
        created = time.time()
        for row in leaderboard_rows(bosses, players_meta):
            await self.db.execute(
                "INSERT INTO aion_leaderboard (guild_id, boss, player, class, damage, dps, run_id, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, boss, player) DO UPDATE SET "
                "class=excluded.class, damage=excluded.damage, dps=excluded.dps, "
                "run_id=excluded.run_id, created=excluded.created "
                "WHERE excluded.dps > aion_leaderboard.dps",
                (guild_id, row["boss"], row["player"], row["class"], row["damage"], row["dps"], run_id, created),
            )

    async def _store_run(
        self, guild_id: int, run: dict, players: list[dict], bosses: list[dict],
        timeline: list[dict], extras: dict, reporters: int,
    ) -> str:
        run_id = secrets.token_urlsafe(16)
        created = time.time()
        data = {
            "dungeon": run["dungeon"], "difficulty": run.get("difficulty"),
            "bosses_total": run["bosses_total"], "bosses_killed": run["bosses_killed"],
            "fight_ms": run["fight_ms"], "created": created, "reporters": reporters,
            "players": players, "bosses": bosses, "timeline": timeline, **extras,
        }
        path = runs_dir(self.db) / f"{run_id}.json"
        text = json.dumps(data)

        def _write():
            path.write_text(text)

        await asyncio.get_running_loop().run_in_executor(None, _write)  # Timeline kann einige MB groß sein
        await self.db.execute(
            "INSERT INTO aion_runs (id, guild_id, created, dungeon, difficulty, reporters) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, guild_id, created, run["dungeon"], run.get("difficulty"), reporters),
        )
        return run_id

    # ---- Einrichtung -----------------------------------------------------------------------

    @app_commands.command(name="aion", description="AION-2-Duplikatfilter für DeckBar-Posts einrichten")
    @app_commands.describe(
        kanal="Kanal für den Duplikatfilter bzw. die zusammengeführten AION-Posts",
        wartezeit="Sekunden, die auf weitere Posts vom selben Lauf gewartet wird (Standard 15)",
        token="Neuen API-Token erzeugen (alten ungültig machen), für die DeckBar-API-Anbindung",
        aus="Duplikatfilter abschalten",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def aion_settings(
        self, interaction: discord.Interaction, kanal: discord.TextChannel | None = None,
        wartezeit: app_commands.Range[int, 5, 300] | None = None, token: bool = False, aus: bool = False,
    ):
        if aus:
            await self.db.set(interaction.guild_id, "aion_channel", None)
            return await interaction.response.send_message("AION-Duplikatfilter ist aus.", ephemeral=True)
        if kanal:
            await self.db.set(interaction.guild_id, "aion_channel", kanal.id)
        if wartezeit:
            await self.db.set(interaction.guild_id, "aion_dedup_delay", wartezeit)

        lines = []
        channel_id = self.db.get_int(interaction.guild_id, "aion_channel")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        delay = self.db.get_int(interaction.guild_id, "aion_dedup_delay") or DEFAULT_DELAY
        if not channel:
            return await interaction.response.send_message(
                "Noch kein Kanal festgelegt. `/aion kanal:#dein-deckbar-kanal`.", ephemeral=True
            )
        lines.append(f"Kanal: {channel.mention}, Wartezeit {delay}s.")
        lines.append(
            "Variante 1 (DeckBar unverändert): postet weiter selbst per eigenem Webhook in diesen Kanal, "
            "der Bot löscht Duplikate."
        )

        api_token = self.db.get(interaction.guild_id, "aion_api_token")
        if token or not api_token:
            api_token = secrets.token_urlsafe(24)
            await self.db.set(interaction.guild_id, "aion_api_token", api_token)
        lines.append(
            f"\nVariante 2 (API, braucht eine DeckBar-Änderung):\n`POST {self.api_url()}`\n"
            f"`Authorization: Bearer {api_token}`\n"
            "Geheim halten, nur an DeckBar-Instanzen geben, die mitmachen sollen. Mit `token:True` "
            "einen neuen erzeugen (macht den alten ungültig)."
        )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot):
    await bot.add_cog(AionDedup(bot))
