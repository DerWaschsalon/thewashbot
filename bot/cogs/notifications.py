"""🔔 Benachrichtigungen: GitHub (später weitere Dienste) per Klick-Assistent einrichten.

Hängt am selben Klick-Panel wie die anderen /setup-Einstellungen (siehe settings.py, Button
„Benachrichtigungen“) — kein eigener Kanal, keine eigenen Befehle. Die Discord-Seite hier kennt
nur `bot.notify.base.Provider`, nichts GitHub-Spezifisches; ein neuer Dienst kommt in
`bot/notify/manager.py` dazu und taucht hier automatisch in der Dienst-Auswahl auf.

Hintergrund-Abfrage: `poll_feeds` prüft alle paar Minuten jeden aktiven Feed (siehe
`bot/notify/manager.py` für Dedup/Fehlerbehandlung).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import discord
from discord.ext import commands, tasks

from ..notify.base import ProviderError
from ..notify.manager import PROVIDERS, NotificationManager

log = logging.getLogger(__name__)
POLL_MINUTES = max(1, int(os.getenv("NOTIFY_POLL_MINUTES", "10")))


@dataclass
class Draft:
    """Zustand eines laufenden Einrichtungs-Assistenten, nur für die Dauer der Interaktion."""

    cog: "Notifications"
    guild: discord.Guild
    persistent: bool
    feed_id: int | None = None  # gesetzt beim Bearbeiten eines bestehenden Feeds
    kind: str = ""
    source: str = ""
    events: list[str] = field(default_factory=list)
    channel_id: int | None = None
    filters: dict = field(default_factory=dict)


def _tl(persistent: bool) -> int | None:
    return None if persistent else 180


def feed_summary(feed) -> str:
    provider = PROVIDERS.get(feed["kind"])
    events = json.loads(feed["events"] or "[]")
    names = [provider.events.get(e, e) for e in events] if provider else events
    return "\n".join(f"• {n}" for n in names) or "–"


def feed_embed(guild: discord.Guild, feed) -> discord.Embed:
    provider = PROVIDERS.get(feed["kind"])
    channel = guild.get_channel(feed["channel_id"])
    e = discord.Embed(
        title=f"🔔 {provider.label if provider else feed['kind']}",
        description=f"**{feed['source']}**", colour=discord.Colour.blurple(),
    )
    e.add_field(name="Ereignisse", value=feed_summary(feed), inline=False)
    e.add_field(name="Ziel", value=channel.mention if channel else f"(gelöscht: {feed['channel_id']})")
    e.add_field(name="Status", value="🟢 Aktiv" if feed["enabled"] else "⏸️ Deaktiviert")
    filters = json.loads(feed["filters"] or "{}")
    if filters:
        parts = []
        if filters.get("labels"):
            parts.append("Labels: " + ", ".join(filters["labels"]))
        if filters.get("author"):
            parts.append(f"Autor: {filters['author']}")
        if "prereleases" in filters:
            parts.append(f"Pre-Releases: {'ja' if filters['prereleases'] else 'nein'}")
        if "drafts" in filters:
            parts.append(f"Drafts: {'ja' if filters['drafts'] else 'nein'}")
        e.add_field(name="Filter", value="\n".join(parts) or "keine", inline=False)
    if feed["error"]:
        e.add_field(name="⚠️ Letzter Fehler", value=feed["error"][:500], inline=False)
    return e


# ---- Schritt 1: Dienst -----------------------------------------------------------------------


class ServiceSelect(discord.ui.Select):
    def __init__(self, draft: Draft):
        super().__init__(
            placeholder="Dienst wählen …",
            options=[discord.SelectOption(label=p.label, value=p.key) for p in PROVIDERS.values()],
            row=0, custom_id="notif:select:service",
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        self.draft.kind = self.values[0]
        provider = PROVIDERS[self.draft.kind]
        await interaction.response.send_modal(RepoModal(self.draft, provider))


class ServiceSelectView(discord.ui.View):
    def __init__(self, draft: Draft):
        super().__init__(timeout=_tl(draft.persistent))
        self.add_item(ServiceSelect(draft))
        self.add_item(HomeButton(draft))


class RepoModal(discord.ui.Modal):
    def __init__(self, draft: Draft, provider):
        super().__init__(title=f"{provider.label}: {provider.source_label}")
        self.draft = draft
        self.provider = provider
        self.source_input = discord.ui.TextInput(
            label=provider.source_label, placeholder=provider.source_example, max_length=200,
            default=draft.source or None,
        )
        self.add_item(self.source_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            source = await self.provider.validate_source(str(self.source_input.value))
        except ProviderError as e:
            retry = discord.ui.View(timeout=_tl(self.draft.persistent))
            retry_btn = discord.ui.Button(label="Nochmal versuchen", emoji="🔁", style=discord.ButtonStyle.primary)

            async def _retry(inner: discord.Interaction):
                await inner.response.send_modal(RepoModal(self.draft, self.provider))

            retry_btn.callback = _retry
            retry.add_item(retry_btn)
            retry.add_item(HomeButton(self.draft))
            await interaction.response.edit_message(
                embed=discord.Embed(title="❌ Ungültiges Repository", description=str(e), colour=discord.Colour.red()),
                view=retry,
            )
            return
        self.draft.source = source
        embed = discord.Embed(
            title="🔔 Neue Benachrichtigung – Ereignisse", colour=discord.Colour.blurple(),
            description=f"**{self.draft.source}**\n\nWelche Ereignisse sollen gemeldet werden?",
        )
        await interaction.response.edit_message(embed=embed, view=EventSelectView(self.draft))


# ---- Schritt 2: Ereignisse --------------------------------------------------------------------


class EventSelect(discord.ui.Select):
    def __init__(self, draft: Draft):
        provider = PROVIDERS[draft.kind]
        super().__init__(
            placeholder="Ereignisse wählen (Mehrfachauswahl) …",
            min_values=1, max_values=len(provider.events),
            options=[
                discord.SelectOption(label=label, value=key, default=key in draft.events)
                for key, label in provider.events.items()
            ],
            row=0, custom_id="notif:select:events",
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        self.draft.events = list(self.values)
        view = ChannelStepView(self.draft)
        embed = discord.Embed(
            title="🔔 Neue Benachrichtigung – Ziel-Kanal", colour=discord.Colour.blurple(),
            description=f"**{self.draft.source}**\n\nIn welchen Kanal sollen die Meldungen gehen?",
        )
        await interaction.response.edit_message(embed=embed, view=view)


class EventSelectView(discord.ui.View):
    def __init__(self, draft: Draft):
        super().__init__(timeout=_tl(draft.persistent))
        self.add_item(EventSelect(draft))
        self.add_item(HomeButton(draft))


# ---- Schritt 3: Ziel-Kanal ----------------------------------------------------------------


class NotifChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, draft: Draft):
        super().__init__(placeholder="Kanal wählen …", channel_types=[discord.ChannelType.text], row=0,
                         custom_id="notif:select:channel")
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        # ChannelSelect liefert ein "leichtes" AppCommandChannel ohne permissions_for() —
        # den echten, gecachten Kanal fürs Rechte-Prüfen holen.
        channel = interaction.guild.get_channel(self.values[0].id) or self.values[0]
        if hasattr(channel, "permissions_for") and not channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(
                f"Ich darf in {channel.mention} nicht schreiben. Bitte einen anderen Kanal wählen "
                "oder mir dort Schreibrechte geben.", ephemeral=True,
            )
        self.draft.channel_id = channel.id
        if self.draft.feed_id is not None:
            await save_feed(interaction, self.draft)
            return
        view = FilterStepView(self.draft)
        embed = discord.Embed(
            title="🔔 Neue Benachrichtigung – Filter (optional)", colour=discord.Colour.blurple(),
            description=f"**{self.draft.source}** → {channel.mention}\n\n"
                        "Optional einschränken, oder direkt weiter ohne Filter.",
        )
        await interaction.response.edit_message(embed=embed, view=view)


class ChannelStepView(discord.ui.View):
    def __init__(self, draft: Draft):
        super().__init__(timeout=_tl(draft.persistent))
        self.add_item(NotifChannelSelect(draft))
        self.add_item(HomeButton(draft))


# ---- Schritt 4: Filter (optional) --------------------------------------------------------------


class FilterModal(discord.ui.Modal, title="Filter (optional)"):
    def __init__(self, draft: Draft):
        super().__init__()
        self.draft = draft
        issues_or_prs = any(e.startswith(("issue_", "pr_")) for e in draft.events)
        if issues_or_prs:
            self.labels = discord.ui.TextInput(
                label="Labels (kommagetrennt, leer = alle)", required=False, max_length=200,
                default=", ".join(draft.filters.get("labels", [])),
            )
            self.author = discord.ui.TextInput(
                label="Nur dieser GitHub-Login (leer = alle)", required=False, max_length=100,
                default=draft.filters.get("author", ""),
            )
            self.add_item(self.labels)
            self.add_item(self.author)
        if "release" in draft.events:
            self.prereleases = discord.ui.TextInput(
                label="Pre-Releases einschließen? (ja/nein)", required=False, max_length=5,
                default="ja" if draft.filters.get("prereleases", True) else "nein",
            )
            self.drafts = discord.ui.TextInput(
                label="Drafts einschließen? (ja/nein)", required=False, max_length=5,
                default="ja" if draft.filters.get("drafts", True) else "nein",
            )
            self.add_item(self.prereleases)
            self.add_item(self.drafts)

    async def on_submit(self, interaction: discord.Interaction):
        filters: dict = {}
        if hasattr(self, "labels") and str(self.labels.value).strip():
            filters["labels"] = [x.strip() for x in str(self.labels.value).split(",") if x.strip()]
        if hasattr(self, "author") and str(self.author.value).strip():
            filters["author"] = str(self.author.value).strip()
        if hasattr(self, "prereleases"):
            filters["prereleases"] = str(self.prereleases.value).strip().lower() not in ("nein", "no", "n")
        if hasattr(self, "drafts"):
            filters["drafts"] = str(self.drafts.value).strip().lower() not in ("nein", "no", "n")
        self.draft.filters = filters
        await save_feed(interaction, self.draft)


class FilterStepView(discord.ui.View):
    def __init__(self, draft: Draft):
        super().__init__(timeout=_tl(draft.persistent))
        self.draft = draft
        filter_btn = discord.ui.Button(label="Filter setzen", emoji="🔧", style=discord.ButtonStyle.secondary,
                                       row=0, custom_id="notif:filter_set")
        filter_btn.callback = self._open_modal
        self.add_item(filter_btn)
        skip_btn = discord.ui.Button(label="Weiter ohne Filter", emoji="⏭️", style=discord.ButtonStyle.primary,
                                     row=0, custom_id="notif:filter_skip")
        skip_btn.callback = self._skip
        self.add_item(skip_btn)
        self.add_item(HomeButton(draft))

    async def _open_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(FilterModal(self.draft))

    async def _skip(self, interaction: discord.Interaction):
        await save_feed(interaction, self.draft)


# ---- Speichern + Zusammenfassung ---------------------------------------------------------------


async def save_feed(interaction: discord.Interaction, draft: Draft) -> None:
    db = draft.cog.db
    if draft.feed_id is None:
        feed_id = await db.execute(
            "INSERT INTO feeds (guild_id, channel_id, kind, source, events, filters, enabled, state) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, '{}')",
            (draft.guild.id, draft.channel_id, draft.kind, draft.source,
             json.dumps(draft.events), json.dumps(draft.filters)),
        )
    else:
        feed_id = draft.feed_id
        # events/Kanal geändert → Zustand zurücksetzen, sonst würde beim nächsten Abgleich alles
        # zu den neuen Ereignissen auf einen Schlag als "neu" gemeldet.
        await db.execute(
            "UPDATE feeds SET channel_id=?, events=?, filters=?, state='{}', error=NULL WHERE id=?",
            (draft.channel_id, json.dumps(draft.events), json.dumps(draft.filters), feed_id),
        )
    feed = await db.fetchone("SELECT * FROM feeds WHERE id=?", (feed_id,))
    await draft.cog.manager.poll_feed(feed, draft.cog.bot)  # stiller Baseline-Abgleich, postet nichts

    embed = discord.Embed(title="✅ Gespeichert", colour=discord.Colour.green(), description=feed_summary(feed))
    channel = draft.guild.get_channel(draft.channel_id)
    embed.add_field(name="Ziel", value=channel.mention if channel else "–")
    view = NotificationsHomeView(draft.cog, draft.guild, draft.persistent)
    await interaction.response.edit_message(embed=embed, view=view)


# ---- Verwaltung ----------------------------------------------------------------------------


class ManageSelect(discord.ui.Select):
    def __init__(self, cog: "Notifications", guild: discord.Guild, persistent: bool, feeds: list):
        super().__init__(
            placeholder="Benachrichtigung wählen …",
            options=[
                discord.SelectOption(
                    label=f"{PROVIDERS.get(f['kind']).label if PROVIDERS.get(f['kind']) else f['kind']}: "
                          f"{f['source']}"[:100],
                    value=str(f["id"]),
                    description=("🟢 Aktiv" if f["enabled"] else "⏸️ Deaktiviert")[:100],
                )
                for f in feeds[:25]
            ],
            row=0, custom_id="notif:select:manage",
        )
        self.cog, self.guild, self.persistent = cog, guild, persistent

    async def callback(self, interaction: discord.Interaction):
        feed = await self.cog.db.fetchone("SELECT * FROM feeds WHERE id=?", (int(self.values[0]),))
        if not feed:
            return await interaction.response.send_message("Die gibt es nicht mehr.", ephemeral=True)
        await interaction.response.edit_message(
            embed=feed_embed(self.guild, feed), view=FeedDetailView(self.cog, self.guild, self.persistent, feed)
        )


class ManageListView(discord.ui.View):
    def __init__(self, cog: "Notifications", guild: discord.Guild, persistent: bool, feeds: list):
        super().__init__(timeout=_tl(persistent))
        if feeds:
            self.add_item(ManageSelect(cog, guild, persistent, feeds))
        draft = Draft(cog, guild, persistent)
        self.add_item(HomeButton(draft))


class FeedDetailView(discord.ui.View):
    def __init__(self, cog: "Notifications", guild: discord.Guild, persistent: bool, feed):
        super().__init__(timeout=_tl(persistent))
        self.cog, self.guild, self.persistent, self.feed = cog, guild, persistent, feed

        edit_btn = discord.ui.Button(label="Bearbeiten", emoji="✏️", style=discord.ButtonStyle.secondary, row=0)
        edit_btn.callback = self._edit
        self.add_item(edit_btn)

        toggle_btn = discord.ui.Button(
            label="Deaktivieren" if feed["enabled"] else "Aktivieren",
            emoji="⏸️" if feed["enabled"] else "▶️", style=discord.ButtonStyle.secondary, row=0,
        )
        toggle_btn.callback = self._toggle
        self.add_item(toggle_btn)

        test_btn = discord.ui.Button(label="Testbenachrichtigung", emoji="🧪", style=discord.ButtonStyle.secondary, row=1)
        test_btn.callback = self._test
        self.add_item(test_btn)

        delete_btn = discord.ui.Button(label="Löschen", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
        delete_btn.callback = self._delete
        self.add_item(delete_btn)

        back_btn = discord.ui.Button(label="Zurück", emoji="⬅️", style=discord.ButtonStyle.secondary, row=2)
        back_btn.callback = self._back
        self.add_item(back_btn)

    async def _back(self, interaction: discord.Interaction):
        feeds = await self.cog.db.fetchall("SELECT * FROM feeds WHERE guild_id=?", (self.guild.id,))
        await interaction.response.edit_message(
            embed=manage_overview_embed(feeds),
            view=ManageListView(self.cog, self.guild, self.persistent, feeds),
        )

    async def _edit(self, interaction: discord.Interaction):
        provider = PROVIDERS.get(self.feed["kind"])
        draft = Draft(
            self.cog, self.guild, self.persistent, feed_id=self.feed["id"], kind=self.feed["kind"],
            source=self.feed["source"], events=json.loads(self.feed["events"] or "[]"),
            channel_id=self.feed["channel_id"], filters=json.loads(self.feed["filters"] or "{}"),
        )
        embed = discord.Embed(
            title="✏️ Benachrichtigung bearbeiten", colour=discord.Colour.blurple(),
            description=f"**{self.feed['source']}** ({provider.label if provider else self.feed['kind']})\n\n"
                        "Ereignisse und Ziel-Kanal neu wählen (Filter bleiben wie bisher, einfach "
                        "noch mal speichern lässt sie unverändert).",
        )
        await interaction.response.edit_message(embed=embed, view=EventSelectView(draft))

    async def _toggle(self, interaction: discord.Interaction):
        new_state = 0 if self.feed["enabled"] else 1
        await self.cog.db.execute("UPDATE feeds SET enabled=? WHERE id=?", (new_state, self.feed["id"]))
        feed = await self.cog.db.fetchone("SELECT * FROM feeds WHERE id=?", (self.feed["id"],))
        if feed is None:
            return await interaction.response.send_message("Die gibt es nicht mehr.", ephemeral=True)
        await interaction.response.edit_message(
            embed=feed_embed(self.guild, feed), view=FeedDetailView(self.cog, self.guild, self.persistent, feed)
        )

    async def _delete(self, interaction: discord.Interaction):
        from .cleanup import Confirm  # lokal, um einen Zyklus settings<->notifications<->cleanup zu vermeiden

        view = Confirm(interaction.user.id, "Löschen")
        await interaction.response.send_message(
            f"**{self.feed['source']}** wirklich löschen?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.confirmed:
            return
        await self.cog.db.execute("DELETE FROM feeds WHERE id=?", (self.feed["id"],))
        await interaction.edit_original_response(content="✅ Gelöscht.", view=None)
        # Auch das eigentliche Panel aktualisieren, sonst zeigt es noch Buttons für den
        # gelöschten Eintrag an.
        feeds = await self.cog.db.fetchall("SELECT * FROM feeds WHERE guild_id=?", (self.guild.id,))
        await interaction.message.edit(
            embed=manage_overview_embed(feeds), view=ManageListView(self.cog, self.guild, self.persistent, feeds)
        )

    async def _test(self, interaction: discord.Interaction):
        provider = PROVIDERS.get(self.feed["kind"])
        if provider is None:
            return await interaction.response.send_message("Unbekannter Dienst.", ephemeral=True)
        channel = self.guild.get_channel(self.feed["channel_id"])
        if not channel:
            return await interaction.response.send_message("Ziel-Kanal nicht gefunden.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            notification = await provider.test_notification(self.feed["source"], json.loads(self.feed["events"] or "[]"))
        except ProviderError as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        notification.embed.add_field(name="🧪 Hinweis", value="Dies ist eine Testbenachrichtigung von TheWashbot.", inline=False)
        try:
            await channel.send(embed=notification.embed)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"❌ Senden in {channel.mention} fehlgeschlagen: {e}", ephemeral=True)
        await interaction.followup.send(f"✅ Testbenachrichtigung in {channel.mention} gesendet.", ephemeral=True)


def manage_overview_embed(feeds: list) -> discord.Embed:
    e = discord.Embed(title="⚙️ Benachrichtigungen verwalten", colour=discord.Colour.blurple())
    if not feeds:
        e.description = "Noch keine Benachrichtigung eingerichtet."
    else:
        e.description = f"{len(feeds)} eingerichtet. Zum Bearbeiten unten auswählen."
    return e


# ---- Hauptmenü ------------------------------------------------------------------------------


class HomeButton(discord.ui.Button):
    """Zurück zum 🔔-Hauptmenü (aus jedem Schritt des Assistenten)."""

    def __init__(self, draft: Draft, row: int = 4):
        super().__init__(label="Abbrechen", emoji="⬅️", style=discord.ButtonStyle.secondary, row=row)
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=home_embed(), view=NotificationsHomeView(self.draft.cog, self.draft.guild, self.draft.persistent)
        )


def home_embed() -> discord.Embed:
    return discord.Embed(
        title="🔔 Benachrichtigungen", colour=discord.Colour.blurple(),
        description="Hier kannst du festlegen, über welche Ereignisse TheWashbot informieren soll.",
    )


class NotificationsHomeView(discord.ui.View):
    def __init__(self, cog: "Notifications", guild: discord.Guild, persistent: bool):
        super().__init__(timeout=_tl(persistent))
        self.cog, self.guild, self.persistent = cog, guild, persistent

    @discord.ui.button(label="Neue Benachrichtigung", emoji="➕", style=discord.ButtonStyle.success, row=0,
                       custom_id="notif:open:new")
    async def new(self, interaction: discord.Interaction, _button: discord.ui.Button):
        draft = Draft(self.cog, self.guild, self.persistent)
        await interaction.response.edit_message(
            embed=discord.Embed(title="🔔 Neue Benachrichtigung – Dienst", colour=discord.Colour.blurple(),
                                description="Welchen Dienst willst du überwachen?"),
            view=ServiceSelectView(draft),
        )

    @discord.ui.button(label="Benachrichtigungen verwalten", emoji="⚙️", style=discord.ButtonStyle.secondary, row=0,
                       custom_id="notif:open:manage")
    async def manage(self, interaction: discord.Interaction, _button: discord.ui.Button):
        feeds = await self.cog.db.fetchall("SELECT * FROM feeds WHERE guild_id=?", (self.guild.id,))
        await interaction.response.edit_message(
            embed=manage_overview_embed(feeds), view=ManageListView(self.cog, self.guild, self.persistent, feeds)
        )

    @discord.ui.button(label="Aktualisieren", emoji="🔄", style=discord.ButtonStyle.secondary, row=0,
                       custom_id="notif:open:refresh")
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        feeds = await self.cog.db.fetchall(
            "SELECT * FROM feeds WHERE guild_id=? AND enabled=1", (self.guild.id,)
        )
        posted = 0
        for feed in feeds:
            result = await self.cog.manager.poll_feed(feed, self.cog.bot)
            if result:
                posted += len(result)
        await interaction.followup.send(
            f"{len(feeds)} aktive Benachrichtigung(en) geprüft, {posted} neue Meldung(en) gepostet.",
            ephemeral=True,
        )

    @discord.ui.button(label="Zurück zu den Einstellungen", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1,
                       custom_id="notif:open:settings_back")
    async def back_to_settings(self, interaction: discord.Interaction, _button: discord.ui.Button):
        from .settings import SettingsPanel  # lokal, um einen Import-Zyklus zu vermeiden

        setup_cog = interaction.client.get_cog("Setup")
        panel = SettingsPanel(setup_cog, self.guild, persistent=self.persistent)
        await interaction.response.edit_message(embed=panel.embed(), view=panel)


class Notifications(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.manager = NotificationManager(bot.db)
        self.poll_feeds.start()

    def cog_unload(self):
        self.poll_feeds.cancel()

    @tasks.loop(minutes=POLL_MINUTES)
    async def poll_feeds(self):
        feeds = await self.db.fetchall("SELECT * FROM feeds WHERE enabled=1")
        for feed in feeds:
            try:
                await self.manager.poll_feed(feed, self.bot)
            except Exception:
                log.exception("Feed %s (%s) konnte nicht abgefragt werden", feed["id"], feed["source"])

    @poll_feeds.before_loop
    async def before_poll_feeds(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Notifications(bot))
