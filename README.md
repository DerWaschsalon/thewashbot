# Discord-Bot: Musik, Rollen, Moderation, Temp-Voice, Aufräumen

Ein Bot für alles, was ihr bisher mit mehreren Bots macht. Alle Befehle sind Slash-Befehle
(`/…`). Antworten, die nur dich betreffen, sieht auch nur du, damit die Kanäle sauber bleiben.

## Funktionen

| Bereich | Befehle | Was passiert |
|---|---|---|
| **Musik** | `/play`, `/skip`, `/stop`, `/pause`, `/weiter`, `/warteschlange`, `/jetzt`, `/lautstaerke`, `/wiederholen`, `/mischen`, `/entfernen` | YouTube-Links und -Suche, SoundCloud, Spotify-Links (Titel, Alben, Playlists, gesucht auf YouTube). Eine „Läuft gerade“-Nachricht mit Buttons, die alte wird gelöscht. Allein im Kanal geht der Bot nach 1 Min., bei leerer Warteschlange nach 5 Min. |
| **Radio** | `/radio sender:` oder `/radio url:` | Zehntausende Sender aus radio-browser.info, Vorschläge beim Tippen. Oder direkt eine Stream-Adresse. |
| **Rollen** | `/rollen-panel` | Nachricht mit Buttons, über die man sich Rollen selbst nimmt oder abgibt. Funktioniert auch nach Neustarts. |
| **Willkommen** | `/setup willkommen`, `/setup autorolle` | Begrüßung mit Platzhaltern `{user}`, `{name}`, `{server}`, `{anzahl}`, Rolle für Neue |
| **Verifizierung** | `/setup verifizierung`, `/setup regeln-text` | Panel mit Regeln + „Regeln akzeptiert“-Button. Aktiv: @everyone sieht den Server erst nach Bestätigen, nur der Regel-Kanal bleibt sichtbar. Autorolle (z. B. „Neuling“) kommt trotzdem sofort beim Beitritt. `/setup regeln-text` ändert den Text im Panel nachträglich. Mit Vorschau/Bestätigung, `aus:True` schaltet wieder ab. |
| **Moderation** | `/setup log-kanal`, `/setup spamschutz`, `/timeout` | Log für gelöschte/bearbeitete Nachrichten, Beitritte (mit Warnung bei neuen Konten), Austritte, Banns, Rollen. Spamschutz: Flut, Massen-Erwähnungen, Einladungslinks. |
| **Verwarnungen** | `/warn`, `/warnungen`, `/warnung-loeschen`, `/kick`, `/ban`, `/unban`, `/setup verwarnungen` | Verwarnungen mit Historie (DM an Betroffene), automatische Strafe ab einstellbarer Schwelle (Timeout/Kick/Ban). |
| **Temp-Voice** | `/setup temp-voice`, `/vc name/limit/sperren/freigeben/einladen/uebernehmen` | Wer „➕ Kanal erstellen“ betritt, bekommt einen eigenen Sprachkanal, der leer wieder verschwindet. |
| **Aufräumen** | `/aufraeumen nachrichten`, `/aufraeumen bots`, `/aufraeumen bericht` | Alte Nachrichten löschen (Filter nach Alter, Person, nur Bots), alte Bots entfernen, Bericht über tote Kanäle und leere Rollen. Immer erst mit Vorschau und Bestätigung. |
| **Erinnerungen** | `/erinnere wann: text: privat:` | Zeit relativ (`2h`, `30m`, `1d2h`) oder absolut (`18:30`, `24.12. 18:00`). `/erinnerungen` zeigt eigene offene Erinnerungen, `/erinnerung-loeschen` entfernt eine. |
| **Tickets** | `/setup tickets rolle:` (fügt hinzu/entfernt, mehrere möglich; im Klick-Panel per Mehrfachauswahl), `/ticket-panel` | Panel mit Buttons (❓ Frage, ⚠️ Meldung, 📋 Bewerbung) → Modal → privater Thread mit allen Team-Rollen. „Schließen“ im Thread beendet ihn, sperrt ihn und schickt den Verlauf als `.txt` in den Log-Kanal. |
| **DeckBar-Gruppen-DPS** | `/setup dps-webhook`, auch im Klick-Panel | Nimmt den Gruppen-Endstand von [DeckBar](https://github.com/gamunex/deckbar) nach jedem Kampf entgegen und postet ihn als Embed (eine Nachricht pro Gruppe, nicht pro Spieler). Braucht Port 8787 in `docker-compose.yml` und `DPS_WEBHOOK_HOST` in `.env`. |
| **Benachrichtigungen** | Button „🔔 Benachrichtigungen" im Klick-Panel | GitHub-Repos beobachten (Releases, Issues, Pull Requests, Commits) per Klick-Assistent, komplett über Buttons/Select-Menüs/Modals, kein Befehl nötig. Modular für weitere Dienste (YouTube, Twitch, RSS, …) — siehe `bot/notify/`. |
| **Web-Dashboard** | `/setup dashboard rolle:`, dann `http://<DASHBOARD_HOST>:8789` | Einstellungen im Browser mit Discord-Login, nur für die freigeschaltete Rolle. Siehe „Web-Dashboard einrichten" unten. |
| **AION-2** | `/aion kanal:`, `/aion token:True` | Mehrere DeckBar-Posts vom selben Dungeon-Lauf zu einem zusammenführen — per Duplikatfilter (ohne DeckBar-Änderung) oder per API mit Rangliste (braucht DeckBar-Änderung). Siehe „AION-2: mehrere DeckBar-Posts zu einem zusammenführen" unten. |
| **DJ-Rolle** | `/setup dj-rolle` | Optional: Dann dürfen nur DJ, Admins und wer den Song gewünscht hat, überspringen und stoppen. |
| **Bump-Erinnerung** | `/setup bump kanal: rolle:`, auch im Klick-Panel | Erkennt die Bump-Bestätigung von [DISBOARD](https://disboard.org) im festgelegten Kanal und erinnert nach Ablauf des 2-Stunden-Cooldowns per Ping (optionale Rolle) daran, `/bump` erneut zu tippen. Rein passiv, der Bot bumpt nicht selbst. |
| **Mitglieder-Zähler** | `/setup mitglieder-zaehler kanal:`, auch im Klick-Panel | Name eines Sprachkanals zeigt laufend die aktuelle Mitgliederzahl (z. B. „👥 Mitglieder: 123“), aktualisiert bei Beitritt/Austritt und mindestens alle 10 Minuten (Discords Limit für Kanal-Umbenennungen). |
| **Starboard** | `/setup starboard kanal: schwelle:`, auch im Klick-Panel | Nachrichten ab X ⭐-Reaktionen (ohne die des Autors selbst, Standard 3) landen als Embed in einem Highlight-Kanal, Sterne-Zähler aktualisiert sich live, Post verschwindet wieder, wenn die Reaktionen unter die Schwelle fallen. |
| **Bots-Zähler** | `/setup bots-zaehler kanal:`, auch im Klick-Panel | Zweiter, unabhängig einstellbarer Sprachkanal zeigt die Bot-Anzahl (z. B. „🤖 Bots: 12“). |
| **Voice-Statistik** | `/sprachzeit`, XP per `/setup sprachzeit-xp` oder im Klick-Panel | Zeit im Sprachkanal läuft mit (nicht allein, nicht im AFK-Kanal, nicht selbst taub/stumm), optional zusätzlich XP pro Minute für ein künftiges Levelsystem. |
| **Umfragen** | `/umfrage frage: antworten: dauer_stunden: mehrfachauswahl:` | Native Discord-Umfrage, 2–10 Antworten durch `\|` getrennt. |
| **Geburtstage** | `/geburtstag-setzen tag: monat:`, `/geburtstag-loeschen`, `/geburtstag`, Kanal per `/setup geburtstage` oder im Klick-Panel | Tägliche Gratulation um 9 Uhr (Europe/Berlin) im eingerichteten Kanal. |
| **Embed-Ersteller** | `/embed-erstellen kanal:` | Formular (Titel, Beschreibung, Hex-Farbe, Bild-URL), postet das fertige Embed. |
| **Eigene Befehle** | `/befehl-erstellen name: antwort:`, `/befehl-loeschen`, `/befehle` | `!name` im Chat antwortet mit dem hinterlegten Text, Platzhalter `{user}`/`{server}`. |
| **Zeitgesteuerte Ankündigungen** | `/ankuendigung-erstellen kanal: text: intervall_minuten:`, `/ankuendigungen`, `/ankuendigung-loeschen` | Text wird automatisch alle X Minuten in einen Kanal gepostet. |
| **Benachrichtigungen: YouTube** | Dienst „YouTube" im Klick-Assistenten (🔔) | Neue Videos per öffentlichem RSS-Feed, ohne API-Schlüssel — braucht die Kanal-ID (beginnt mit „UC…"). |
| **Giveaways** | `/giveaway-erstellen kanal: preis: dauer: gewinner:`, `/giveaway-reroll` | Embed mit „🎉 Teilnehmen"-Button (An-/Abmelden), automatische Auslosung nach Ablauf, Reroll für neue Gewinner. `dauer` im selben Format wie bei `/erinnere` (z. B. `2h`, `1d2h`). |
| **Levelsystem** | `/rang`, `/rangliste`, `/level-xp-geben`, `/level-belohnung`, `/level-kanal-ohne-xp`, `/level-rolle-ohne-xp`, an/aus per `/setup level` oder im Klick-Panel | 15–25 XP pro Nachricht (60 s Abklingzeit), Rangkarte als Bild, Level-Up-Nachricht, kumulative Rollen-Belohnungen. Standardmäßig **aus**, bis ein Server es einschaltet. |
| **Server-Status** | `/server`, `/server-neustart container:`, `/server-update` — nur für `OWNER_ID` | CPU/RAM/Uptime/Festplatte, laufende Container. `/server-update` zieht das neueste Image von GHCR und ersetzt den Bot-Container live damit (kurze Downtime). Braucht den Docker-Socket gemountet (in `docker-compose.yml` auskommentiert, siehe „Server-Status einrichten" unten) — **Root-Zugriff auf den Host**, deshalb bewusst nur eine einzelne Person statt einer Rolle. |
| **Automod erweitert** | `/setup automod`, auch im Klick-Panel | Wortfilter, Großbuchstaben-Filter, Emoji-Spam-Grenze, Link-Filter mit erlaubten Domains — jeder Verstoß zählt zusätzlich als automatische Verwarnung (zählt mit Richtung der automatischen Strafschwelle). |

`/setup anzeigen` zeigt alle Einstellungen, `/setup panel` öffnet sie zum Anklicken (Kanal-/Rollen-
Auswahl per Dropdown, Text per Formular, Ein/Aus per Knopf — kein Befehl tippen nötig). Das
Klick-Panel deckt inzwischen **alle** `/setup`-Einstellungen ab, inklusive Verwarnungs-Schwelle,
Web-Dashboard-Rolle und der kompletten Verifizierungs-Ersteinrichtung (mit Vorschau/Bestätigung
wie beim Befehl).
`/setup management-kanal kategorie:` legt einen festen Kanal mit genau diesem Klick-Panel an (z. B.
unter einer internen Admin-Kategorie) — bleibt dauerhaft da und funktioniert auch nach einem
Neustart des Bots weiter. Die `/setup`- und `/aufraeumen`-Befehle sehen nur Leute mit
„Server verwalten“.

## 1. Bot bei Discord anlegen

1. <https://discord.com/developers/applications> → **New Application**, Name z. B. eures Servers
2. Links **Bot** → **Reset Token** → Token kopieren (kommt in `.env`, niemals teilen)
3. Auf derselben Seite unter **Privileged Gateway Intents** einschalten:
   **Server Members Intent** und **Message Content Intent**
4. Bot einladen mit diesem Link (Client-ID von der Seite **OAuth2** einsetzen):

   ```
   https://discord.com/oauth2/authorize?client_id=DEINE_CLIENT_ID&scope=bot+applications.commands&permissions=1099800374290
   ```

   Die Rechte: Kanäle sehen, Nachrichten senden/verwalten/Verlauf lesen, Links einbetten, Dateien,
   externe Emojis, Sprache verbinden/sprechen/verschieben, Kanäle und Rollen verwalten, kicken,
   Timeout.
5. Nach dem Einladen in den **Servereinstellungen → Rollen** die Rolle des Bots **über** die Rollen
   ziehen, die er vergeben soll (Autorolle, Rollen-Panel) und über die Bots, die er entfernen soll.

## 2. Auf dem Home-Server starten (Docker)

Das Image wird von GitHub Actions gebaut und nach `ghcr.io/navonex/thewashbot` gepusht (bei jedem
Push auf `main`, der diesen Ordner betrifft). `docker-compose.yml` zieht dieses fertige Image,
kein Bauen mehr auf dem Server nötig.

```sh
cp .env.example .env        # DISCORD_TOKEN und DEV_GUILD_ID eintragen
docker compose pull
docker compose up -d
docker compose logs -f      # "Angemeldet als …" und "… Slash-Befehle registriert"
```

Ist das GHCR-Paket `thewashbot` **privat** (Standard bei privaten Repos), braucht `docker compose
pull` vorher einen Login mit einem GitHub-PAT (Scope `read:packages`):

```sh
echo "<PAT>" | docker login ghcr.io -u navonex --password-stdin
```

Alternative: Paket-Sichtbarkeit auf GitHub (Package-Einstellungen → Change visibility → Public)
umstellen, dann entfällt der Login.

**Unraid:** Ordner z. B. nach `/mnt/user/appdata/discord-bot` kopieren und dort im Terminal die
Befehle oben ausführen (Plugin „Docker Compose Manager“), oder das Image bauen und als Container
mit `/data` → `/mnt/user/appdata/discord-bot/data` und den Variablen aus `.env` anlegen.

Die Einstellungen liegen in `data/bot.sqlite3`. Bei jedem Start aktualisiert der Container
yt-dlp, weil YouTube regelmäßig etwas ändert. Spielt YouTube nicht mehr: Container neu starten.

## 3. Einrichten (in Discord)

```
/setup log-kanal kanal:#mod-log
/setup willkommen kanal:#willkommen text:Hey {user}, willkommen auf {server}! Schau in #regeln vorbei.
/setup autorolle rolle:@Mitglied
/setup temp-voice                       (legt Kategorie + „➕ Kanal erstellen“ an)
/rollen-panel rollen:🎮 @Gamer 🎵 @Musik 📢 @News titel:Wähl deine Rollen
/setup tickets rolle:@Team
/ticket-panel titel:Support text:Klick auf einen Button, um ein Ticket zu eröffnen.
```

## Aufräumen: so gehen wir vor

1. `/aufraeumen bericht` → du bekommst eine `.md`-Datei mit allen Kanälen (letzte Aktivität),
   Rollen (wie viele Mitglieder) und Bots. Die kannst du mir schicken, dann machen wir gemeinsam
   einen Plan, was weg kann.
2. `/aufraeumen bots` → Liste aller Bots, Auswahl der alten, Bestätigen. 🔒 heißt: Der Bot steht
   über meiner Rolle, also erst die Rolle verschieben.
3. `/aufraeumen nachrichten kanal:#musik nur_bots:True` → z. B. alle alten Ausgaben der früheren
   Musikbots weg. Mit `aelter_als_tage:` bleibt Neues stehen, angepinnte Nachrichten bleiben
   standardmäßig. Nachrichten älter als 14 Tage löscht Discord nur einzeln, das dauert länger.

## Gut zu wissen

- **YouTube:** Das Abspielen von YouTube über Bots verstößt gegen die YouTube-AGB, deshalb wurden
  große Bots wie Rythm abgeschaltet. Für einen privaten Server ist das Risiko gering, aber es kann
  sein, dass YouTube die IP zeitweise blockiert („Sign in to confirm you're not a bot“). Dann hilft
  eine Cookies-Datei (`YTDLP_COOKIES`, siehe yt-dlp-Doku), am besten von einem Zweitkonto.
- **Spotify** liefert kein Audio. Der Bot liest Titel und Interpret und sucht sie auf YouTube.
  Ohne API-Schlüssel liest er die öffentliche Einbettungsseite (bis ~100 Titel). Von Spotify selbst
  erstellte Playlists (z. B. „Today's Top Hits“) gibt die API seit Ende 2024 nicht mehr heraus.
- **Spamschutz** ignoriert Bots und alle mit „Nachrichten verwalten“.

## Benachrichtigungen (GitHub) einrichten

Im Klick-Panel (`/setup panel` oder `/setup management-kanal`) → Button **🔔 Benachrichtigungen**
→ **➕ Neue Benachrichtigung**: Dienst (aktuell nur GitHub) → Repository (wird geprüft) →
Ereignisse (Mehrfachauswahl: Releases, neue/geschlossene Issues, neue/geschlossene/gemergte Pull
Requests, Commits) → Ziel-Kanal → optionale Filter (Labels, Autor, Pre-Releases/Drafts) →
Zusammenfassung mit Speichern/Abbrechen. Danach sofort ein stiller erster Abgleich (postet nichts,
verhindert, dass alte Releases/Issues/PRs beim Einrichten als „neu" durchgehen).

Unter **⚙️ Benachrichtigungen verwalten**: pro Eintrag Bearbeiten (Ereignisse/Kanal), De-/Aktivieren,
Löschen, **🧪 Testbenachrichtigung** (schickt eine echte Beispiel-Meldung in den Zielkanal — prüft
damit auch, ob der Bot dort wirklich schreiben darf). **🔄 Aktualisieren** fragt sofort ab, statt
auf den nächsten Durchlauf zu warten (`NOTIFY_POLL_MINUTES`, Standard 10).

Discussions sind bewusst nicht dabei (nur über GitHub GraphQL abrufbar, nicht über die hier
genutzte REST-API). Ohne `GITHUB_TOKEN` in der `.env` erlaubt GitHub nur 60 Anfragen/Stunde
**insgesamt** — reicht nur für wenige Repos bei 10-Minuten-Takt. Mit einem Token (nur Lesen nötig,
github.com/settings/tokens) sind es 5000/Stunde.

Architektur für weitere Dienste: `bot/notify/base.py` (Schnittstelle `Provider`), `bot/notify/github.py`
(erster Provider), `bot/notify/manager.py` (Registry + Abfrage/Dedup/Fehlerbehandlung). Ein neuer
Dienst (YouTube, Twitch, RSS, …) implementiert nur `Provider` und trägt sich in `PROVIDERS` ein —
die Discord-Oberfläche in `bot/cogs/notifications.py` kennt nichts Dienst-Spezifisches.

## AION-2: mehrere DeckBar-Posts zu einem zusammenführen

Wenn mehrere Leute gemeinsam denselben Dungeon laufen und jeder sein eigenes
[DeckBar](https://github.com/gamunex/deckbar) hat, postet sonst jeder seinen eigenen Dungeon-Run
in Discord. Zwei unabhängige Wege, je nachdem ob DeckBar angepasst wird:

### Variante 1: ohne DeckBar zu ändern

Der Bot behält nur den zuerst angekommenen Post, löscht die restlichen — kein Eingriff in DeckBar,
kein eigenes Gruppensystem/API/WebUI.

1. In Discord: Kanal-Einstellungen → Integrationen → Webhook erstellen → URL kopieren (normale
   Discord-Funktion, nicht vom Bot).
2. Diese Webhook-URL trägt **jeder** in seinem eigenen DeckBar ein: Web-UI → Karte
   „Dungeon-Runs → Discord“ (oder `~/.config/deckbar/runs.yaml`, Feld `webhook`). DeckBar postet
   dann wie gewohnt selbst, ganz ohne den Bot zu kennen.
3. `/aion kanal:#der-kanal` in Discord — ab da beobachtet der Bot diesen Kanal und filtert Duplikate.
   Geht auch über das Klick-Panel (`/setup panel` → „AION-2-Filter") oder im Web-Dashboard unter
   „AION-2-Filter".

Erkennung: gleicher Dungeon-Name, gleiche Boss-Anzahl/-Siege, Kampfzeit und Startzeit nah beieinander
(DeckBars Text hat nur Minuten-Genauigkeit bei der Startzeit). Ein Spieler-Filter nach Namen ist
hier **nicht** möglich — die Spielernamen stehen nur im angehängten Bild, nicht im Text der Nachricht.

### Variante 2: API (braucht eine Änderung an DeckBar)

Statt Discord-Posten zu filtern, schickt DeckBar strukturierte Lauf-Daten (echte Spielernamen,
Schaden, DPS) an einen Endpunkt auf diesem Server. Der Bot merged mehrere Meldungen desselben
Laufs zu einer Rangliste und postet einmal selbst ein Embed (🥇🥈🥉-Format). Braucht eine Änderung
an DeckBar (bei `daemon/deckbar_daemon/runs.py`, dort wo `post_discord` aufgerufen wird), die noch
nicht umgesetzt ist — siehe `HANDOFF.md`.

1. `AION_PUBLIC_URL` in der `.env` setzen (öffentliche Adresse, z. B. per Cloudflare Tunnel auf
   `DASHBOARD_PORT`, ohne Pfad/Slash am Ende) und `/aion kanal:` wie oben einrichten.
2. `/aion token:True` in Discord — erzeugt bzw. erneuert den API-Token und zeigt die volle
   Endpunkt-URL. Geheim halten, nur an teilnehmende DeckBar-Instanzen geben.
3. Jede teilnehmende DeckBar-Installation sendet pro abgeschlossenem Lauf:

   ```
   POST <AION_PUBLIC_URL>/api/aion/fight
   Authorization: Bearer <Token aus /aion token:True>
   Content-Type: application/json

   {
     "dungeon": "Ferocious Horn Den", "difficulty": "Normal",
     "bosses_total": 3, "bosses_killed": 3,
     "start_ms": 1760445123000, "fight_ms": 350000,
     "players": [{"name": "ReZero", "damage": 13290000, "dps": 38000}, …],
     "bosses": [                                         // optional, für DPS pro Boss
       {"name": "Boss 1", "players": [{"name": "ReZero", "damage": 4200000, "dps": 41000}, …]},
       …
     ]
   }
   ```

   `bosses` ist optional — ohne das Feld zeigt das Embed nur die Gesamt-Rangliste, mit dem Feld
   zusätzlich ein eigenes Feld pro Boss mit der DPS je Spieler für genau diesen Kampf.

Beide Varianten laufen unabhängig nebeneinander — schadet nichts, beide gleichzeitig eingerichtet
zu haben.

## Web-Dashboard einrichten

Einstellungen im Browser statt in Discord, Login mit dem eigenen Discord-Konto. Deckt die
Einstellungen ab, die einfache Werte sind (Kanäle, Rollen, Texte, An/Aus); den mehrstufigen
Assistenten für neue GitHub-Benachrichtigungen gibt's bewusst nur in Discord.

1. Im Developer Portal unter **OAuth2** → **Redirects** genau diese URL eintragen:
   `http://<DASHBOARD_HOST>:<DASHBOARD_PORT>/callback` (Standard-Port 8789).
2. `.env`: `DISCORD_CLIENT_ID` (General Information → Application ID), `DISCORD_CLIENT_SECRET`
   (OAuth2 → Client Secret, **geheim**), `DASHBOARD_HOST` (LAN-IP dieses Servers), `DASHBOARD_PORT`.
3. `/setup dashboard rolle:@Admin` in Discord — nur Mitglieder mit dieser Rolle kommen rein (ohne
   das: nur wer „Server verwalten“ hat).
4. Port 8789 ist in `docker-compose.yml` schon freigegeben. Aufrufen: `http://<DASHBOARD_HOST>:8789`.

Unter **Sichtbarkeit** gibt's zusätzlich eine Matrix (Kanal/Kategorie × Rolle): zeigt live, wer was
sehen kann, Klick auf eine Zelle schaltet die Sichtbarkeit sofort um (nur „Sehen“, keine anderen
Rechte). Zeigt nur echte Mitglieder-Rollen, keine bot-eigenen Integrations-Rollen.

Läuft über HTTP ohne eigenes TLS — nur fürs LAN/Tailscale gedacht, nicht fürs offene Internet.
Sessions gehen bei jedem Neustart des Bots verloren (dann einfach neu einloggen).

## DeckBar-Gruppen-DPS einrichten

Postet nach jedem Kampf den Endstand der eigenen Gruppe als eine Nachricht (nicht pro Spieler
einzeln). Setzt eine [DeckBar](https://github.com/gamunex/deckbar)-Installation voraus, die den
Gruppen-Stand bei Kampfende an eine Webhook-URL schickt (Teil der DeckBar-Einrichtung, nicht
dieses Repos).

1. In `docker-compose.yml` ist Port 8787 schon freigegeben, `.env`: `DPS_WEBHOOK_HOST=<LAN-IP
   dieses Servers>` eintragen (z. B. `192.168.1.20`), damit die angezeigte URL stimmt.
2. `/setup dps-webhook kanal:#dps` in Discord ausführen (oder im Klick-Panel unter „DPS-Webhook“) →
   Antwort enthält die URL (`http://<HOST>:8787/dps/<Token>`), die in die DeckBar-Einstellungen gehört.
3. URL geheim halten — wer sie hat, kann in diesem Kanal posten.

## Server-Status einrichten

`/server` (Status, laufende Container), `/server-neustart container:` (ein anderer Container neu
starten) und `/server-update` (den Bot-Container selbst mit dem neuesten GHCR-Image aktualisieren)
brauchen Zugriff auf den Docker-Daemon des Hosts — das ist **Root-Zugriff auf diesen Server**,
deshalb bewusst nicht standardmäßig aktiv und auf eine einzelne Person beschränkt statt auf eine
Rolle.

1. In `docker-compose.yml` die auskommentierte Zeile beim `volumes:`-Block einkommentieren:
   `- /var/run/docker.sock:/var/run/docker.sock`
2. In der `.env`: `OWNER_ID=<deine Discord-User-ID>` eintragen (Rechtsklick auf dich selbst in
   Discord → ID kopieren, braucht den Entwicklermodus). Nur diese Person sieht die drei Befehle.
3. `docker compose up -d` — danach sind `/server`, `/server-neustart`, `/server-update` aktiv.

**Wie `/server-update` funktioniert:** zieht `ghcr.io/navonex/thewashbot:latest`, liest die
laufende Container-Konfiguration aus (Umgebungsvariablen, Volumes, Ports, Restart-Policy) und
startet damit einen kurzlebigen Helfer-Container (`bot/selfupdate_helper.py`), der wartet, bis
der alte Bot-Container gestoppt ist, ihn entfernt und den neuen mit identischer Konfiguration,
aber dem neuen Image anlegt. Läuft als eigener Container, weil sich ein laufender Prozess nicht
mitten im eigenen Lauf selbst ersetzen kann. Dauert ein paar Sekunden, in denen der Bot offline
ist. **Falls dabei etwas schiefgeht:** auf unraid-server `cd /mnt/user/appdata/thewashbot && docker
compose up -d` stellt den Bot anhand der `docker-compose.yml` wieder her — die bleibt die ganze
Zeit unverändert auf der Platte liegen, als Netz.

## Entwickeln und testen

```sh
pip install -r requirements.txt pytest
python -m pytest -q tests          # ohne Discord-Verbindung
DISCORD_TOKEN=… DB_PATH=./data/bot.sqlite3 python -m bot
```
