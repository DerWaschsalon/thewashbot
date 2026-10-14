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

## Noch zu bauen (Daves Auswahl)

Gewählt:
- **Tickets**: Panel mit Buttons (Frage, Meldung, Bewerbung) → Modal → privater Thread mit Team-Rolle,
  „Schließen“-Button, Verlauf als .txt in den Log-Kanal.
- **Voice-Statistik**: Zeit im Sprachkanal (nicht allein, nicht AFK/taub), optional XP pro Minute.
- **Starboard**: ⭐ ab Schwelle (Standard 3) → Highlight-Kanal, Zähler aktualisieren.
- **Web-Dashboard**: aiohttp im selben Prozess. Login per `/dashboard` (Einmal-Link mit signiertem
  Token, 10 Min. gültig, nur „Server verwalten“), kein OAuth nötig. Formulare aus einem zentralen
  Schema (geplant: `bot/schema.py` mit Feldern key/label/type/section und Listen für
  custom_commands, scheduled, feeds, level_rewards), damit `/setup` und Dashboard dieselben Schlüssel nutzen.
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
- **Benachrichtigungen**: YouTube per RSS (ohne Schlüssel), Twitch nur mit TWITCH_CLIENT_ID/SECRET
- **Statistik-Kanäle** (Mitglieder, Bots; Umbenennen höchstens alle 10 Min.)
- **Umfragen** (native Discord-Polls), **Giveaways** (Button, Reroll), **Geburtstage** (tägl. 9 Uhr
  Europe/Berlin), **Embed-Ersteller** (Modal)

Abhängigkeiten dafür: `pillow`, `tzdata` in requirements.txt ergänzen.
Discord erlaubt höchstens 100 Top-Level-Slash-Befehle; neue Funktionen als Gruppen anlegen
(`/level …`, `/mod …`, `/ticket …`) und die Grenze im Test prüfen.
