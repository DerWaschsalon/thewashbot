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
| **Web-Dashboard** | `/setup dashboard rolle:`, dann `http://<DASHBOARD_HOST>:8788` | Einstellungen im Browser mit Discord-Login, nur für die freigeschaltete Rolle. Siehe „Web-Dashboard einrichten" unten. |
| **DJ-Rolle** | `/setup dj-rolle` | Optional: Dann dürfen nur DJ, Admins und wer den Song gewünscht hat, überspringen und stoppen. |

`/setup anzeigen` zeigt alle Einstellungen, `/setup panel` öffnet sie zum Anklicken (Kanal-/Rollen-
Auswahl per Dropdown, Text per Formular, Ein/Aus per Knopf — kein Befehl tippen nötig).
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

## Web-Dashboard einrichten

Einstellungen im Browser statt in Discord, Login mit dem eigenen Discord-Konto. Deckt die
Einstellungen ab, die einfache Werte sind (Kanäle, Rollen, Texte, An/Aus); den mehrstufigen
Assistenten für neue GitHub-Benachrichtigungen gibt's bewusst nur in Discord.

1. Im Developer Portal unter **OAuth2** → **Redirects** genau diese URL eintragen:
   `http://<DASHBOARD_HOST>:<DASHBOARD_PORT>/callback` (Standard-Port 8788).
2. `.env`: `DISCORD_CLIENT_ID` (General Information → Application ID), `DISCORD_CLIENT_SECRET`
   (OAuth2 → Client Secret, **geheim**), `DASHBOARD_HOST` (LAN-IP dieses Servers), `DASHBOARD_PORT`.
3. `/setup dashboard rolle:@Admin` in Discord — nur Mitglieder mit dieser Rolle kommen rein (ohne
   das: nur wer „Server verwalten“ hat).
4. Port 8788 ist in `docker-compose.yml` schon freigegeben. Aufrufen: `http://<DASHBOARD_HOST>:8788`.

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

## Entwickeln und testen

```sh
pip install -r requirements.txt pytest
python -m pytest -q tests          # ohne Discord-Verbindung
DISCORD_TOKEN=… DB_PATH=./data/bot.sqlite3 python -m bot
```
