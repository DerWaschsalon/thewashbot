# Übergabe: Stand und nächste Schritte

Für Claude Code auf dem Server. Bitte zuerst README.md und diese Datei lesen.

## Fertig und getestet (offline, noch nicht gegen echten Discord-Server)

- `bot/__main__.py`: Start, Cog-Liste, Fehlerbehandlung (`UserError` → Antwort nur für den Aufrufer)
- `bot/db.py`: SQLite, Einstellungen als Schlüssel/Wert pro Server (im Speicher gecacht),
  generische `execute/fetchone/fetchall/get_ids`. Die Tabellen für die nächsten Funktionen sind
  schon angelegt (levels, warnings, reminders, starboard, tickets, birthdays, giveaways,
  giveaway_entries, custom_commands, scheduled, feeds, level_rewards).
- Cogs: `music` (yt-dlp, Spotify, Radio über radio-browser.info), `roles` (Willkommen, Autorolle,
  Rollen-Panel), `moderation` (Logs, Spamschutz, /timeout), `tempvoice`, `cleanup`
  (nachrichten/bots/bericht), `settings` (/setup …)
- Docker: `Dockerfile`, `docker-compose.yml`, `.env.example`, `entrypoint.sh` (aktualisiert yt-dlp)
- Tests: `python -m pytest -q tests`

## Als Erstes auf dem Server

1. `.env` anlegen (DISCORD_TOKEN, DEV_GUILD_ID), `docker compose up -d --build`, Logs prüfen
2. Musik, Radio, Rollen-Panel, Temp-Voice, Logs am echten Server ausprobieren, Fehler beheben
3. `/aufraeumen bericht` laufen lassen und mit Dave den Aufräum-Plan machen

## Fertig, aber noch nicht am echten Server getestet

- **Verifizierung**: `/setup verifizierung` (Panel mit Regeln + „Regeln akzeptiert“-Button → Rolle,
  Vorschau/Bestätigung, bestehende Mitglieder werden automatisch mitgenommen, `aus:True` schaltet ab)
- **Klick-Einstellungen**: `/setup panel` (ephemer) und `/setup management-kanal` (fester, dauerhaft
  klickbarer Kanal, überlebt Neustarts über `Setup.register_persistent_views`)
- **Verwarnungen**: `/warn`, `/warnungen`, `/warnung-loeschen`, `/kick`, `/ban`, `/unban`,
  `/setup verwarnungen` (automatische Strafe ab einstellbarer Schwelle: Timeout/Kick/Ban), DM an
  Betroffene. `/clear` und `/slowmode` aus der Liste unten noch offen (deckt sich mit
  `/aufraeumen nachrichten`, ggf. nicht mehr nötig).
- **Erinnerungen**: `/erinnere wann:2h|30m|1d2h|18:30|24.12. 18:00 text:… privat:`, `/erinnerungen`,
  `/erinnerung-loeschen`. Hintergrund-Task prüft alle 30s, alles in Europe/Berlin (`tzdata` in
  requirements.txt).
- **DeckBar-Gruppen-DPS**: `/dps-webhook kanal:` richtet einen Token+Kanal ein, der Bot hört per
  aiohttp auf Port 8787 (`bot/cogs/dps.py`) auf `POST /dps/<token>` und postet ein Embed (ein Post
  pro Gruppe). Gegenstück fehlt noch in DeckBar selbst — siehe Übergabe weiter unten.
- **Benachrichtigungen (GitHub)**: `bot/notify/base.py` (Provider-Schnittstelle),
  `bot/notify/github.py` (Releases/Issues/PRs/Commits per Polling, Dedup über `feeds.state`,
  Discussions bewusst nicht dabei — nur GraphQL), `bot/notify/manager.py` (Registry + Abfrage,
  Fehler landen in `feeds.error`), `bot/cogs/notifications.py` (Klick-Assistent + Verwaltung +
  Testbenachrichtigung, hängt als „🔔 Benachrichtigungen“-Button am Klick-Panel). `feeds`-Tabelle
  in db.py um events/filters/enabled/state/error erweitert (mit Migration für schon bestehende
  Installationen, siehe `Database._migrate`). Nächster Dienst (YouTube/Twitch/RSS/…) muss nur
  `Provider` implementieren und sich in `PROVIDERS` eintragen — die Discord-Seite ändert sich nicht.
- **Tickets**: `bot/cogs/tickets.py`. `/setup tickets rolle:` (auch im Klick-Panel) legt die
  Team-Rolle(n) fest, `/ticket-panel` postet das Panel (Frage/Meldung/Bewerbung → Modal → privater
  Thread, Team-Rolle + Ersteller werden per `thread.add_user` hinzugefügt). „Schließen“ im Thread
  sperrt ihn und schickt den Verlauf als `.txt` in den Log-Kanal (`tickets`-Tabelle für
  Kurzverlauf/Statistik später).
- **Web-Dashboard**: `bot/cogs/dashboard.py`, eigener aiohttp-Server (Port 8789). Login per
  Discord-OAuth2 (nur Scope `identify`, Rolle wird danach über den Bot selbst geprüft — keine
  weiteren Nutzer-Rechte nötig). Zugriff nur für die per `/setup dashboard` festgelegte Rolle
  (Fallback: „Server verwalten“). Deckt alle einfachen Schlüssel/Wert-Einstellungen ab
  (Allgemein, Verifizierung-Text, Tickets, DPS-Webhook, Verwarnungs-Schwelle,
  Benachrichtigungen an/aus/löschen). Bewusst **nicht** abgedeckt: der mehrstufige
  GitHub-Benachrichtigungs-Assistent (bleibt in Discord) und die große
  Verifizierungs-Ersteinrichtung (bleibt `/setup verifizierung` wegen Vorschau/Bestätigung).
  Sessions nur im Speicher (RAM), gehen bei Neustart verloren.

## Noch zu bauen (Daves Auswahl)

Gewählt:
- **Voice-Statistik**: Zeit im Sprachkanal (nicht allein, nicht AFK/taub), optional XP pro Minute.
- **Starboard**: ⭐ ab Schwelle (Standard 3) → Highlight-Kanal, Zähler aktualisieren.
- **Server-Status `/server`** (nur Dave, Env `OWNER_ID`): CPU/RAM/Uptime aus /proc, Plattenplatz
  der gemounteten Unraid-Pfade, Container über den Docker-Socket (optional gemountet, Hinweis: Socket
  = Root-Zugriff). Neustart eines Containers nur mit Bestätigung.

Fehlende MEE6-Funktionen:
- **Levelsystem**: 15–25 XP pro Nachricht, 60 s Abklingzeit, Formel 5·l² + 50·l + 100; Rangkarte
  (Pillow), `/rangliste`, Level-up-Nachricht, Rollen-Belohnungen, Kanäle/Rollen ohne XP, Admin-XP-Befehle
- **Automod erweitert**: Wortfilter, Großbuchstaben, Emoji-Spam, Link-Filter mit erlaubten Domains,
  Verstöße als automatische Verwarnung
- **Eigene Befehle** (Präfix `!`, Platzhalter {user} {server})
- **Zeitgesteuerte Ankündigungen** (Intervall in Minuten)
- **Benachrichtigungen: weitere Dienste** (GitHub ist fertig, siehe oben): YouTube per RSS (ohne
  Schlüssel), Twitch nur mit TWITCH_CLIENT_ID/SECRET — jeweils als neuer `Provider` in `bot/notify/`
- **Statistik-Kanäle** (Mitglieder, Bots; Umbenennen höchstens alle 10 Min.)
- **Umfragen** (native Discord-Polls), **Giveaways** (Button, Reroll), **Geburtstage** (tägl. 9 Uhr
  Europe/Berlin), **Embed-Ersteller** (Modal)

Abhängigkeiten dafür: `pillow`, `tzdata` in requirements.txt ergänzen.
Discord erlaubt höchstens 100 Top-Level-Slash-Befehle; neue Funktionen als Gruppen anlegen
(`/level …`, `/mod …`, `/ticket …`) und die Grenze im Test prüfen.

## Offen: DeckBar-Seite der Gruppen-DPS-Integration

Dave hat mit gamunex (Betreiber von github.com/gamunex/deckbar) abgestimmt, dass DeckBar bei
Kampfende den Gruppen-Stand an den oben beschriebenen Bot-Webhook schickt (eine PR dorthin, wie
bei den bisherigen DeckBar-Änderungen von Dave). Noch zu bauen, in `~/Projekte/deckbar` bzw. im
Fork `Navonex/deckbar`:

- `capture/src/view.rs`: `Row`/`Candidate` um ein `party: bool` ergänzen, aus
  `PersonalData.combat_power != 0` (siehe `capture/a2tools/src-tauri/src/entity/personal_data.rs`:
  combat_power kommt nur aus dem Partei-Roster-Paket, 0 = nicht in der eigenen Gruppe). Die
  aktuelle `build()`-Funktion zeigt aktuell alle erkannten Spieler in der Nähe, nicht nur die
  eigene Gruppe.
- `daemon/deckbar_daemon/dps.py`: `DpsFeed.on_combat`-Callback feuert schon bei Kampfende
  (`active: bool` wird `False`) — dort den letzten Stand (nur `party: true`-Zeilen) per POST an
  die konfigurierte Bot-Webhook-URL schicken.
- Neue Einstellung (Webhook-URL) in `dps.yaml` + Web-UI (`daemon/deckbar_daemon/web/`).
- Payload-Format, das der Bot erwartet: siehe `bot/cogs/dps.py` im thewashbot-Repo
  (`{"target": …, "duration_s": …, "party": [{"name", "cls", "dps", "dmg", "pct"}]}`).

## Offen: DeckBar-Seite der AION-2-Run-Zusammenführung (separates Feature)

Eigenständig von der Gruppen-DPS-Integration oben: Dave will zusätzlich, dass DeckBar nach einem
abgeschlossenen Dungeon-Lauf (nicht nach jedem Kampf) die Lauf-Daten an `/api/aion/fight`
(`bot/cogs/dashboard.py` + `bot/cogs/aion_dedup.py`, Auth per Bearer-Token aus `/aion token:True`)
schickt, damit der Bot mehrere Meldungen vom selben Lauf (verschiedene Spieler, je eigenes DeckBar)
zu einer Rangliste zusammenführt und einmal postet. Bot-Seite ist fertig, DeckBar-Seite noch offen.
Bis dahin funktioniert der einfachere Duplikatfilter ohne DeckBar-Änderung (`/aion kanal:`,
`bot/cogs/aion_dedup.py` on_message) bereits eigenständig.

Ansatzpunkt in DeckBar: `daemon/deckbar_daemon/runs.py`, Funktion `post_discord` wird nach einem
abgeschlossenen `Run` aufgerufen (siehe `DungeonRuns`-Klasse, Zeile ~928). Dort zusätzlich (nicht
statt) ein `POST` an die neue Bot-URL mit dem erwarteten Payload:

```json
{
  "dungeon": "Ferocious Horn Den", "difficulty": "Normal",
  "bosses_total": 3, "bosses_killed": 3,
  "start_ms": 1760445123000, "fight_ms": 350000,
  "players": [{"name": "ReZero", "damage": 13290000, "dps": 38000}],
  "bosses": [
    {"name": "Boss 1", "players": [{"name": "ReZero", "damage": 4200000, "dps": 41000}]}
  ]
}
```

`bosses` ist optional, bringt aber DPS je Boss statt nur den Gesamtwert fürs Embed — pro Boss aus
`Run.bosses()` eine `Fight` mit eigener `players`-Liste (gleiches Format wie oben, aber nur für
diesen einen Kampf). `Run.bosses()` und `Fight.players` liefern die nötigen Werte bereits (siehe
`Run`/`Fight`/`Player`-Dataclasses in `runs.py`). Neue Einstellung (Bot-URL + Token) in `runs.yaml`
+ Web-UI ergänzen, analog zum bestehenden `webhook`-Feld dort.
