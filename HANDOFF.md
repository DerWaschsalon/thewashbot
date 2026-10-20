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
- **Klick-Panel: vollständig**: `bot/cogs/settings.py` deckt jetzt alle `/setup`-Einstellungen ab,
  nicht mehr nur eine Teilmenge. Neu dazugekommen: Verwarnungs-Schwelle+Strafe (`WarnView`),
  Web-Dashboard-Rolle (`DashboardRoleView`), und die komplette Verifizierung inklusive
  Ersteinrichtung mit Vorschau/Bestätigung (`VerifyView` → `VerifySetupChannelSelect` →
  `VerifySetupRoleView` → geteilte Logik `apply_verification`/`turn_off_verification`, auch von
  `/setup verifizierung` selbst genutzt). Jede neue Unteransicht folgt dem bestehenden Muster
  (`timeout=None if panel.persistent else 180`, feste `custom_id`s) und überlebt damit Neustarts
  über den schon vorhandenen `management-kanal`-Mechanismus ohne zusätzliche Änderungen an
  `register_persistent_views`.
- **Bump-Erinnerung (Disboard)**: `bot/cogs/bump.py`. `/setup bump kanal: rolle:` (auch im
  Klick-Panel). Erkennt DISBOARDs Bump-Bestätigungs-Embed (`is_bump_confirmation`, Author-ID
  `302050872383242240`) im festgelegten Kanal, merkt sich den Zeitpunkt (`bump_last`) und erinnert
  nach 2 Stunden (`BUMP_INTERVAL_SECONDS`) per Ping im selben Kanal — `bump_reminded` verhindert
  Mehrfach-Erinnerungen, wird bei der nächsten erkannten Bump-Bestätigung zurückgesetzt. Bewusst
  **nur** eine Erinnerung, kein automatisches Bumpen (Selbst-Bot-artiges Verhalten, möglicher
  Verstoß gegen Disboards eigene Regeln) — so vom Nutzer gewünscht.
- **Mitglieder-Zähler**: `bot/cogs/stats.py`. `/setup mitglieder-zaehler kanal:` (auch im
  Klick-Panel, mit sofortigem Update bei Auswahl). Benennt einen Sprachkanal zu „👥 Mitglieder: N“
  um, bei Beitritt/Austritt sofort (gedrosselt auf höchstens alle 10 Minuten über
  `member_count_last`, Discords Limit für Kanal-Umbenennungen) und zusätzlich per
  `tasks.loop(minutes=10)` als Fallback.
- **Web-Dashboard**: `bot/cogs/dashboard.py`, eigener aiohttp-Server (Port 8789). Login per
  Discord-OAuth2 (nur Scope `identify`, Rolle wird danach über den Bot selbst geprüft — keine
  weiteren Nutzer-Rechte nötig). Zugriff nur für die per `/setup dashboard` festgelegte Rolle
  (Fallback: „Server verwalten“). Optik komplett überarbeitet (2026-10-16), angelehnt an DeckBar
  (`DASHBOARD_CSS` im Modul: dunkler Verlauf, transluzente Karten, blauer Akzent, Pill-Chips statt
  des alten Discord-blauen Headers). Deckt jetzt praktisch alle Bereiche ab: Allgemein, Automod
  (neu), Verwarnungen, Tickets, Community (neu: Mitglieder-/Bots-Zähler, Starboard, Geburtstage,
  Bump-Erinnerung), Levelsystem (neu: an/aus, Announce-Kanal, Voice-XP, Kanäle/Rollen ohne XP,
  Rollen-Belohnungen), zeitgesteuerte Ankündigungen (neu, volle Liste+Anlegen+Löschen), eigene
  Befehle (neu, volle Liste+Anlegen+Löschen), DPS-Webhook, AION-2-Filter, Benachrichtigungen
  (an/aus/löschen), Sichtbarkeits-Matrix. Bewusst **nicht** abgedeckt: der mehrstufige
  Benachrichtigungs-Assistent (bleibt in Discord), die große Verifizierungs-Ersteinrichtung
  (bleibt `/setup verifizierung` wegen Vorschau/Bestätigung), Giveaways (keine Schlüssel/Wert-
  Einstellung) und Server-Status (`OWNER_ID`-only, nicht über die rollenbasierte
  Dashboard-Berechtigung). Beim Umbau auch einen latenten Bug in den Alt-Seiten „Allgemein" und
  neu geschriebenen Seiten gefixt: mehrere `<form>`-Tags auf derselben Seite, die alle zum selben
  Handler posten, haben sich beim Abschicken eines einzelnen Formulars gegenseitig die Felder
  geleert (Browser schicken nur Felder des abgeschickten `<form>`, der Handler hat aber alle
  Felder unconditional gesetzt). Jetzt entweder ein gemeinsames `<form>` um mehrere Karten, oder
  jede Karte mit eigener Route. Sessions nur im Speicher (RAM), gehen bei Neustart verloren.

## Noch zu bauen (Daves Auswahl)

Fertig:
- **Server-Status**: `bot/cogs/server.py` + `bot/serverstatus.py` (reine Hilfsfunktionen,
  ohne discord.py/docker-Importe) + `bot/selfupdate_helper.py` (eigenständiges Skript). `/server`
  (CPU/RAM/Uptime aus `/proc`, Festplatte unter `/data`, laufende Container über den Docker-Socket),
  `/server-neustart container:` (mit Bestätigung), `/server-update` (zieht `ghcr.io/navonex/
  thewashbot:latest`, ersetzt den **eigenen** Container live damit — siehe „Server-Status
  einrichten" im README für den genauen Ablauf). Nur für `OWNER_ID` (Env), nicht für eine Rolle,
  weil der Docker-Socket praktisch Root-Zugriff auf unraid-server ist. Socket-Mount in
  `docker-compose.yml` bewusst auskommentiert, nicht standardmäßig aktiv.
- **Starboard**: `bot/cogs/starboard.py`. `/setup starboard kanal: schwelle:` (auch im
  Klick-Panel). ⭐ ab Schwelle (Standard 3, eigener Stern des Autors zählt nicht) → Embed im
  Highlight-Kanal, Zähler aktualisiert sich live über `on_raw_reaction_add/remove`, Post
  verschwindet wieder, wenn die Reaktionen unter die Schwelle fallen. Nutzt die schon vorhandene
  `starboard`-Tabelle in `db.py`.
- **Voice-Statistik**: `bot/cogs/voicestats.py` (Details siehe unten bei „Fertig“).

Noch offen:
- **Benachrichtigungen: Twitch** — braucht `TWITCH_CLIENT_ID`/`SECRET` (Daves Entscheidung), als
  neuer `Provider` in `bot/notify/` nach demselben Muster wie `youtube.py`

Fertig:
- **Eigene Befehle**: `bot/cogs/customcommands.py`. `!name` im Chat, Platzhalter `{user}`/`{server}`,
  verwaltet über `/befehl-erstellen`, `/befehl-loeschen`, `/befehle`.
- **Zeitgesteuerte Ankündigungen**: `bot/cogs/announcements.py`. `/ankuendigung-erstellen kanal:
  text: intervall_minuten:`, `/ankuendigungen`, `/ankuendigung-loeschen`. Nutzt die schon
  vorhandene `scheduled`-Tabelle.
- **Benachrichtigungen: YouTube**: `bot/notify/youtube.py`, RSS-Feed ohne API-Schlüssel (braucht
  die Kanal-ID, nicht Handle/URL). In `PROVIDERS` eingetragen — Discord-Seite unverändert, wie im
  Architektur-Hinweis oben beschrieben.
- **Statistik-Kanäle**: Mitglieder- UND Bots-Zähler fertig (`bot/cogs/stats.py`, zwei unabhängig
  einstellbare Kanäle).
- **Voice-Statistik**: `bot/cogs/voicestats.py`. Zeit im Sprachkanal (nicht allein, nicht AFK,
  nicht selbst taub) läuft in `levels.voice_seconds`, optional XP/Minute. `/sprachzeit`.
- **Umfragen** (native Discord-Polls): `bot/cogs/polls.py`, `/umfrage`.
- **Geburtstage**: `bot/cogs/birthdays.py`, tägl. 9 Uhr Europe/Berlin.
- **Embed-Ersteller** (Modal): `bot/cogs/embeds.py`, `/embed-erstellen`.
- **Giveaways**: `bot/cogs/giveaways.py`, `/giveaway-erstellen`, `/giveaway-reroll`.
- **Automod erweitert**: in `bot/cogs/moderation.py` (dieselbe Spamschutz-Logik erweitert, nicht
  eigener Cog) — Wortfilter, Großbuchstaben, Emoji-Spam, Link-Filter mit erlaubten Domains, jeder
  Verstoß zusätzlich als automatische Verwarnung (`add_warning`, vom Bot selbst als Moderator,
  zählt auch in Richtung der automatischen Strafschwelle). `/setup automod` bzw. im Klick-Panel.
- **Levelsystem**: `bot/cogs/levels.py` + `bot/cogs/rankcard.py` (Pillow, braucht `fonts-dejavu-core`
  im Docker-Image, siehe Dockerfile). 15–25 XP/Nachricht, 60 s Abklingzeit, Formel 5·l²+50·l+100,
  `/rang` (Rangkarte als Bild, fällt auf reinen Text zurück falls das Bild mal nicht gebaut werden
  kann), `/rangliste` (Top 10), Level-Up-Nachricht (eigener Kanal einstellbar), Rollen-Belohnungen
  (`/level-belohnung`, kumulativ — alle Belohnungen bis zum erreichten Level), Kanäle/Rollen ohne
  XP (`/level-kanal-ohne-xp`, `/level-rolle-ohne-xp`), Admin-XP-Befehl (`/level-xp-geben`, auch
  negativ). Komplett abgeschaltet (`level_system_enabled`), bis ein Server es per `/setup level`
  oder im Klick-Panel einschaltet — kein Server bekommt das Verhalten ungefragt. Teilt sich die
  `levels`-Tabelle mit `bot/cogs/voicestats.py` (dort schon XP/Sekunden pro Minute Sprachzeit).

Abhängigkeiten dafür: `pillow`, `tzdata` in requirements.txt ergänzen.
Discord erlaubt höchstens 100 Top-Level-Slash-Befehle; neue Funktionen als Gruppen anlegen
(`/level …`, `/mod …`, `/ticket …`) und die Grenze im Test prüfen.

## DeckBar-Seite der Gruppen-DPS-Integration: auf dem `modular`-Branch umgesetzt

Update 2026-10-20: `capture/src/view.rs` hat seit `deckbar-capture 0.2` das `party: bool`-Feld
(`combat_power > 0 || gear_score > 0`), und `daemon/deckbar_daemon/bot.py` (gleicher Branch)
schickt den Gruppen-Stand bei Kampfende per POST an die Bot-Webhook-URL — Payload-Format exakt wie
von `bot/cogs/dps.py` erwartet (`{"target": …, "duration_s": …, "party": [{"name","cls","dps",
"dmg","pct"}]}`), geprüft gegen unseren Code. Kein Code auf unserer Seite nötig, nur `/setup
dps-webhook` ausführen und die URL in DeckBars Bot-Modul (`dps_url`) eintragen, sobald der Branch
gemergt ist.

## DeckBar-Seite der AION-2-Run-Zusammenführung: auf dem `modular`-Branch umgesetzt

Update 2026-10-20: gamunex hat das auf dem `modular`-Branch (noch nicht in `main` gemergt)
bereits gebaut — `daemon/deckbar_daemon/bot.py`, Discord-Bot-Modul mit genau diesem Payload
(`run_payload()`), inklusive Settings/Web-UI (`dps_url`/`runs_url`/`runs_token` in `bot.yaml`) und
eigenen Tests. Kein Code auf unserer Seite nötig, Format passt exakt gegen unseren bestehenden
`validate_fight_payload`/`same_api_run` (geprüft, 2026-10-20). Sobald der Branch gemergt ist: nur
noch `/aion token:True` ausführen und die URL + den Token in DeckBars Bot-Modul eintragen.

**Noch offen, für die Analyse-Seite (`/runs/<id>` im Dashboard, siehe unten):** `run_payload()`
müsste zusätzlich ein `"timeline"`-Feld mitschicken — eine Liste von Einzeltreffern
`{"t_ms": …, "player": "…", "skill": "…", "damage": …, "crit": bool, "back": bool}`, `t_ms` relativ
zum Kampfbeginn. DeckBar hat diese Rohdaten bereits (`bossTimeline`/bzw. die pro-Hit-Events, die
auch der Mitschnitt selbst sieht) — das ist noch nicht an `bot.py` angebunden. Ohne `timeline`
funktioniert alles wie bisher (reine Rangliste im Embed), mit `timeline` kommt zusätzlich der
Analyse-Link dazu.

## Neu: Analyse-Seite für AION-2-Läufe (`/runs/<id>`)

Ausgelöst durch die Frage, ob man die eigene Zeitleiste gegen eine von außen (z. B.
[AbyssLogs](https://abysslogs.com), ein community-eigener DPS-Meter/Log-Parser für AION 2) prüfen
kann — dessen öffentliche API (`api.abysslogs.com/v1`, nicht offiziell dokumentiert, aus dem
JS-Bundle der Seite ausgelesen) liefert pro Kampf eine volle Hit-für-Hit-Zeitleiste
(`bossTimeline.events`: `timeMs`, `playerId`, `skillCode`, `damage`, Crit-/Front-Flags). Als Vorbild
für unsere eigene, einfachere Version genommen.

- `bot/cogs/aion_dedup.py`: `validate_fight_payload` akzeptiert jetzt optional `timeline`,
  `players_meta`, `boss_hp`, `heals` (siehe Payload-Beispiel oben). `pick_best_list(reports, key)`
  wählt bei mehreren Meldungen pro Liste die mit den meisten Einträgen (keine Event-genaue Fusion
  mehrerer unabhängiger Mitschnitte). `bucket_dps()`/`timeline_stats()` (inkl. Perfect-/Double-
  Rate)/`skill_breakdown()`/`boss_hp_series()`/`heals_summary()` bauen die Diagramm-/Tabellen-Daten.
  `_store_run()` schreibt alles (potenziell mehrere MB) als Datei unter
  `<DB_PATH-Ordner>/runs/<id>.json` (Index in der neuen Tabelle `aion_runs`), `cleanup_runs` räumt
  nach 30 Tagen auf (`RUN_RETENTION_DAYS`). `build_run_embed()` postet bei vorhandener Zeitleiste
  zusätzlich ein Feld „📊 Analyse" mit dem Link.
- `bot/cogs/dashboard.py`: neue **öffentliche** Route `/runs/{id}` (bewusst ohne Discord-Login —
  der Link kommt aus dem Discord-Post, soll ohne Umweg öffnen, wie AbyssLogs' geteilte Logs).
  Zeigt Schaden/DPS pro Spieler (mit Klasse/Ausrüstung aus `players_meta`, falls vorhanden) und
  pro Boss als Balken; bei vorhandener Zeitleiste zusätzlich DPS-über-Zeit-Diagramm,
  Trefferstatistik (Crit-/Back-Attack-/Perfect-/Double-Rate) und eine aufklappbare
  Skill-Aufschlüsselung pro Spieler (mit Icon, wenn über `skills` aufgelöst); bei `boss_hp` eine
  HP-über-Zeit-Kurve (Berserk-Zeitpunkt erkennbar); bei `heals` eine Heilungsübersicht. Alle
  Zusatzfelder (`players_meta`/`boss_hp`/`heals`/`skills`) sind unabhängig voneinander nutzbar —
  DeckBar kann sie einzeln nachrüsten, nichts davon ist Voraussetzung für ein anderes. Chart.js
  von cdnjs, nur auf dieser Seite geladen. `client_max_size` der Dashboard-App auf 25 MB
  angehoben (Standard wäre 1 MB, reicht für größere Zeitleisten nicht).
- **Skill-Namen/-Icons**: `timeline[].skill` kann roher Code oder schon ein Name sein; das neue
  `skills`-Feld (einmal pro Payload, Schlüssel -> `{name, icon}`, AbyssLogs-Stil — siehe
  `names.timelineIcons` dort) löst ihn auf, `skill_breakdown()` nutzt das für Anzeigename + Icon,
  `merge_skill_maps()` vereint die Tabellen mehrerer Meldungen statt nur die größte zu nehmen.
  `bot/cogs/dashboard.py`: `skill_icon_html()` baut daraus ein `<img>` (nur http(s)-URLs
  zugelassen, da die URL aus einem fremden Payload kommt). Für gamunex: DeckBars A2Tools-Kern hat
  diese Auflösung für die eigene Overlay-Anzeige schon (`get_skill_details`/`skill_icons.json` in
  `daemon/deckbar_daemon/a2ui.py`) — sollte beim Bauen der `timeline` wiederverwendbar sein, ohne
  eine neue Datenquelle zu brauchen. Ohne `skills` zeigt die Seite einfach den rohen `skill`-Wert
  ohne Icon, kein Absturz.
- Getestet (2026-10-20) mit echten Kampfdaten von AbyssLogs (ein öffentliches Beispiel, keine
  eigenen Spielerdaten) als Test-Payload gegen den echten `/api/aion/fight`-Endpunkt auf
  unraid-server — Discord-Post inkl. Analyse-Link und die Seite selbst funktionieren.

## Erweiterung der Analyse-Seite: Bestenliste, Buffs/Debuffs, Versuche (2026-10-20)

Weiter Richtung AbyssLogs-Parität ausgebaut (auf Wunsch: „alles so aufbauen wie in AbyssLogs").

- **Bestenliste** (`/leaderboard/{guild_id}`, ebenfalls öffentlich ohne Login — `guild_id` ist
  keine geheime Information): neue Tabelle `aion_leaderboard` (`guild_id, boss, player` als
  Primärschlüssel) hält pro Spieler/Boss den besten je gemeldeten DPS-Wert über alle Läufe hinweg
  fest (`INSERT … ON CONFLICT … DO UPDATE … WHERE excluded.dps > aion_leaderboard.dps`, reines
  Upsert-wenn-besser). `leaderboard_rows(bosses, players_meta)` baut die Zeilen aus denselben
  Feldern, die auch die Analyse-Seite nutzt. `build_run_embed()` verlinkt bei vorhandenen `bosses`
  zusätzlich zur Analyse-Seite die Bestenliste des Servers im selben „📊 Analyse"-Feld.
- **Buffs/Debuffs pro Treffer**: `timeline[].buffs` (optional, Liste aktiver Buff-/Debuff-Codes
  beim jeweiligen Treffer) wird über dieselbe `skills`-Tabelle aufgelöst wie `timeline[].skill`.
  `buff_uptime(timeline, skills)` baut daraus pro Spieler eine Top-10-Liste mit
  Anwesenheitsrate (Anteil eigener Treffer mit aktivem Buff — keine echte Zeit-Uptime, da nur
  treffergenau gemessen, nicht kontinuierlich). Auf der Analyse-Seite als aufklappbarer
  „Buffs/Debuffs"-Abschnitt pro Spieler, mit Icon wie bei der Skill-Aufschlüsselung.
- **Mehrere Versuche/Trash-Phasen** (`attempts[]`, optional, Top-Level): jeder Eintrag hat
  `label`, optional `killed`/`duration_ms`, eine eigene `players`-Liste und optional eine eigene
  `timeline`. `render_fight_section()` (neuer, wiederverwendbarer Renderer in
  `bot/cogs/dashboard.py`) baut daraus pro Versuch denselben Block wie für den Hauptkampf
  (Schadensbalken + bei vorhandener Zeitleiste DPS-Diagramm/Trefferstatistik/Skill-/Buff-
  Aufschlüsselung) — auf der Analyse-Seite als aufklappbarer „Versuche (n)"-Abschnitt, letzter
  Versuch standardmäßig aufgeklappt.
- Alle drei Felder sind wie gewohnt optional und unabhängig voneinander nutzbar, keines ist
  Voraussetzung für die anderen. 64 Tests grün (`python3 -m pytest -q tests`), kein Regressionen.
