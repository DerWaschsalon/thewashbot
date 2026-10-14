"""/setup: alle Einstellungen des Bots pro Server. Nur für Leute mit „Server verwalten“."""

from __future__ import annotations

import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from .cleanup import Confirm
from .dps import ensure_webhook, webhook_url
from ..errors import UserError

log = logging.getLogger(__name__)

DEFAULT_WELCOME = "Willkommen auf **{server}**, {user}! Schau dich in Ruhe um."
DEFAULT_VERIFY_TEXT = (
    "Lies dir die Regeln hier im Kanal durch. Mit Klick auf **Regeln akzeptiert** bekommst du Zugriff "
    "auf den restlichen Server."
)


def fmt_channel(guild: discord.Guild, channel_id: int | None) -> str:
    if not channel_id:
        return "–"
    ch = guild.get_channel(channel_id)
    return ch.mention if ch else f"(gelöscht: {channel_id})"


def fmt_role(guild: discord.Guild, role_id: int | None) -> str:
    if not role_id:
        return "–"
    role = guild.get_role(role_id)
    return role.mention if role else f"(gelöscht: {role_id})"


# ---- Panel: Einstellungen per Klick statt Slash-Befehl mit Text-Parametern ----------------


class BackButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="Zurück", emoji="⬅️", style=discord.ButtonStyle.secondary, row=4,
                         custom_id="setup:back")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class ClearButton(discord.ui.Button):
    """Button für „aus"/„leeren" neben einer Auswahl, die selbst keine leere Option anbieten kann."""

    def __init__(self, label: str, key: str, panel: "SettingsPanel", row: int = 1):
        super().__init__(label=label, emoji="🚫", style=discord.ButtonStyle.danger, row=row,
                         custom_id=f"setup:clear:{key}")
        self.key = key
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await self.panel.cog.db.set(interaction.guild_id, self.key, None)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class LogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Log-Kanal wählen …", channel_types=[discord.ChannelType.text], row=0,
                         custom_id="setup:select:log_channel")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        # ChannelSelect liefert ein "leichtes" AppCommandChannel ohne permissions_for() —
        # den echten, gecachten Kanal fürs Rechte-Prüfen holen.
        channel = interaction.guild.get_channel(self.values[0].id) or self.values[0]
        if hasattr(channel, "permissions_for") and not channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"Ich darf in {channel.mention} nicht schreiben.", ephemeral=True)
        await self.panel.cog.db.set(interaction.guild_id, "log_channel", channel.id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class LogChannelView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(LogChannelSelect(panel))
        self.add_item(ClearButton("Logs aus", "log_channel", panel))
        self.add_item(BackButton(panel))


class WelcomeChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Willkommenskanal wählen …", channel_types=[discord.ChannelType.text], row=0,
                         custom_id="setup:select:welcome_channel")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await self.panel.cog.db.set(interaction.guild_id, "welcome_channel", self.values[0].id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class WelcomeTextModal(discord.ui.Modal, title="Begrüßungstext"):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__()
        self.panel = panel
        current = panel.cog.db.get(panel.guild.id, "welcome_text", DEFAULT_WELCOME)
        self.text = discord.ui.TextInput(
            label="Begrüßungstext",
            placeholder="Platzhalter: {user} {name} {server} {anzahl}",
            style=discord.TextStyle.paragraph, default=current, max_length=1000,
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        await self.panel.cog.db.set(interaction.guild_id, "welcome_text", str(self.text.value))
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class WelcomeTextButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="Text bearbeiten", emoji="✏️", style=discord.ButtonStyle.secondary, row=1,
                         custom_id="setup:welcome_text_btn")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(WelcomeTextModal(self.panel))


class WelcomeView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(WelcomeChannelSelect(panel))
        self.add_item(WelcomeTextButton(panel))
        self.add_item(ClearButton("Willkommen aus", "welcome_channel", panel))
        self.add_item(BackButton(panel))


class AutoroleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Autorolle wählen …", row=0, custom_id="setup:select:autorole")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        if role.managed or role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "Diese Rolle kann ich nicht vergeben. Meine Rolle muss in den Servereinstellungen über ihr stehen.",
                ephemeral=True,
            )
        await self.panel.cog.db.set(interaction.guild_id, "autorole", role.id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class AutoroleView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(AutoroleSelect(panel))
        self.add_item(ClearButton("Autorolle aus", "autorole", panel))
        self.add_item(BackButton(panel))


class DjRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="DJ-Rolle wählen …", row=0, custom_id="setup:select:dj_role")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await self.panel.cog.db.set(interaction.guild_id, "dj_role", self.values[0].id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class DjRoleView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(DjRoleSelect(panel))
        self.add_item(ClearButton("DJ-Rolle aus (alle dürfen steuern)", "dj_role", panel))
        self.add_item(BackButton(panel))


class TicketTeamRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Team-Rolle für Tickets wählen …", row=0, custom_id="setup:select:ticket_team_role")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await self.panel.cog.db.set(interaction.guild_id, "ticket_team_role", self.values[0].id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class TicketTeamRoleView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(TicketTeamRoleSelect(panel))
        self.add_item(ClearButton("Team-Rolle aus", "ticket_team_role", panel))
        self.add_item(BackButton(panel))


class TempVoiceSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Vorhandenen Sprachkanal wählen …", channel_types=[discord.ChannelType.voice], row=0,
                         custom_id="setup:select:tempvoice")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await self.panel.cog.db.set(interaction.guild_id, "tempvoice_hub", self.values[0].id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class TempVoiceCreateButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="Neuen Kanal anlegen", emoji="➕", style=discord.ButtonStyle.secondary, row=1,
                         custom_id="setup:tempvoice_create")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild.me.guild_permissions.manage_channels:
            return await interaction.response.send_message("Mir fehlt das Recht „Kanäle verwalten“.", ephemeral=True)
        category = await guild.create_category("Sprachkanäle")
        channel = await guild.create_voice_channel("➕ Kanal erstellen", category=category)
        await self.panel.cog.db.set(interaction.guild_id, "tempvoice_hub", channel.id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class TempVoiceView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(TempVoiceSelect(panel))
        self.add_item(TempVoiceCreateButton(panel))
        self.add_item(ClearButton("Temp-Voice aus", "tempvoice_hub", panel))
        self.add_item(BackButton(panel))


class DpsWebhookSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Kanal für DeckBar-Gruppen-DPS wählen …", channel_types=[discord.ChannelType.text],
                         row=0, custom_id="setup:select:dps_webhook")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        token = await ensure_webhook(self.panel.cog.db, interaction.guild_id, channel.id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)
        await interaction.followup.send(
            f"Gruppen-DPS geht jetzt nach {channel.mention}.\n\nURL für die DeckBar-Einstellungen:\n"
            f"`{webhook_url(token)}`\n\nGeheim halten — wer die URL hat, kann hier posten.",
            ephemeral=True,
        )


class DpsWebhookView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(DpsWebhookSelect(panel))
        self.add_item(BackButton(panel))


class AntispamView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.panel = panel
        active = panel.cog.db.get_bool(panel.guild.id, "antispam", True)
        allow_invites = panel.cog.db.get_bool(panel.guild.id, "allow_invites")
        toggle = discord.ui.Button(
            label=f"Spamschutz: {'an' if active else 'aus'} (klicken zum Umschalten)",
            emoji="🛡️", style=discord.ButtonStyle.success if active else discord.ButtonStyle.secondary, row=0,
            custom_id="setup:toggle:antispam",
        )
        toggle.callback = self._toggle_antispam
        self.add_item(toggle)
        invites = discord.ui.Button(
            label=f"Einladungslinks: {'erlaubt' if allow_invites else 'werden gelöscht'} (klicken zum Umschalten)",
            emoji="🔗", style=discord.ButtonStyle.success if allow_invites else discord.ButtonStyle.secondary, row=1,
            custom_id="setup:toggle:invites",
        )
        invites.callback = self._toggle_invites
        self.add_item(invites)
        self.add_item(BackButton(panel))

    async def _toggle_antispam(self, interaction: discord.Interaction):
        active = self.panel.cog.db.get_bool(interaction.guild_id, "antispam", True)
        await self.panel.cog.db.set(interaction.guild_id, "antispam", not active)
        await interaction.response.edit_message(embed=self.panel.embed(), view=AntispamView(self.panel))

    async def _toggle_invites(self, interaction: discord.Interaction):
        allowed = self.panel.cog.db.get_bool(interaction.guild_id, "allow_invites")
        await self.panel.cog.db.set(interaction.guild_id, "allow_invites", not allowed)
        await interaction.response.edit_message(embed=self.panel.embed(), view=AntispamView(self.panel))


class SettingsPanel(discord.ui.View):
    """Hauptmenü. `persistent=True`: fest in einem Kanal gepostet, überlebt Neustarts (siehe
    `Setup.register_persistent_views`); sonst normale ephemere Antwort auf `/setup panel`."""

    def __init__(self, cog: "Setup", guild: discord.Guild, *, persistent: bool = False):
        super().__init__(timeout=None if persistent else 180)
        self.cog = cog
        self.guild = guild
        self.persistent = persistent
        if not persistent:
            close = discord.ui.Button(label="Schließen", emoji="✖️", style=discord.ButtonStyle.danger, row=2)
            close.callback = self._close
            self.add_item(close)

    def embed(self) -> discord.Embed:
        return self.cog.overview_embed(self.guild)

    async def _close(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Einstellungen geschlossen.", embed=None, view=None)

    @discord.ui.button(label="Log-Kanal", emoji="📝", style=discord.ButtonStyle.secondary, row=0,
                       custom_id="setup:open:log_channel")
    async def log_channel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=LogChannelView(self))

    @discord.ui.button(label="Willkommen", emoji="👋", style=discord.ButtonStyle.secondary, row=0,
                       custom_id="setup:open:welcome")
    async def welcome(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=WelcomeView(self))

    @discord.ui.button(label="Autorolle", emoji="🎭", style=discord.ButtonStyle.secondary, row=0,
                       custom_id="setup:open:autorole")
    async def autorole(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=AutoroleView(self))

    @discord.ui.button(label="Temp-Voice", emoji="🔊", style=discord.ButtonStyle.secondary, row=1,
                       custom_id="setup:open:tempvoice")
    async def tempvoice(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=TempVoiceView(self))

    @discord.ui.button(label="DJ-Rolle", emoji="🎧", style=discord.ButtonStyle.secondary, row=1,
                       custom_id="setup:open:dj_role")
    async def dj_role(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=DjRoleView(self))

    @discord.ui.button(label="Spamschutz", emoji="🛡️", style=discord.ButtonStyle.secondary, row=1,
                       custom_id="setup:open:antispam")
    async def antispam(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=AntispamView(self))

    @discord.ui.button(label="DPS-Webhook", emoji="⚔️", style=discord.ButtonStyle.secondary, row=2,
                       custom_id="setup:open:dps_webhook")
    async def dps_webhook(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=DpsWebhookView(self))

    @discord.ui.button(label="Benachrichtigungen", emoji="🔔", style=discord.ButtonStyle.secondary, row=2,
                       custom_id="setup:open:notifications")
    async def notifications(self, interaction: discord.Interaction, _button: discord.ui.Button):
        from .notifications import NotificationsHomeView, home_embed

        notif_cog = self.cog.bot.get_cog("Notifications")
        await interaction.response.edit_message(
            embed=home_embed(), view=NotificationsHomeView(notif_cog, self.guild, self.persistent)
        )

    @discord.ui.button(label="Tickets", emoji="🎫", style=discord.ButtonStyle.secondary, row=2,
                       custom_id="setup:open:ticket_team_role")
    async def tickets(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=TicketTeamRoleView(self))


# ---- Verifizierung: Regeln-Panel, das neue Mitglieder erst bestätigen müssen ---------------


class VerifyPanelView(discord.ui.View):
    """Persistent über `custom_id` – wird im `on_interaction`-Listener unten behandelt, nicht hier."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Regeln akzeptiert", emoji="✅", style=discord.ButtonStyle.success, custom_id="verify:accept",
        ))


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class Setup(commands.GroupCog, group_name="setup", group_description="Einstellungen des Bots"):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    def overview_embed(self, guild: discord.Guild) -> discord.Embed:
        db = self.db
        e = discord.Embed(title="Einstellungen", colour=discord.Colour.blurple())
        e.add_field(name="Log-Kanal", value=fmt_channel(guild, db.get_int(guild.id, "log_channel")))
        e.add_field(name="Willkommenskanal", value=fmt_channel(guild, db.get_int(guild.id, "welcome_channel")))
        e.add_field(name="Autorolle", value=fmt_role(guild, db.get_int(guild.id, "autorole")))
        e.add_field(name="Temp-Voice", value=fmt_channel(guild, db.get_int(guild.id, "tempvoice_hub")))
        e.add_field(name="DJ-Rolle", value=fmt_role(guild, db.get_int(guild.id, "dj_role")))
        e.add_field(name="Spamschutz", value="an" if db.get_bool(guild.id, "antispam", True) else "aus")
        e.add_field(name="Einladungslinks", value="erlaubt" if db.get_bool(guild.id, "allow_invites") else "werden gelöscht")
        e.add_field(name="DPS-Webhook (DeckBar)", value=fmt_channel(guild, db.get_int(guild.id, "dps_webhook_channel")))
        e.add_field(name="Ticket-Team-Rolle", value=fmt_role(guild, db.get_int(guild.id, "ticket_team_role")))
        e.add_field(name="Willkommenstext", value=db.get(guild.id, "welcome_text", DEFAULT_WELCOME), inline=False)
        return e

    @app_commands.command(name="anzeigen", description="Alle Einstellungen anzeigen")
    async def show(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self.overview_embed(interaction.guild), ephemeral=True)

    @app_commands.command(name="panel", description="Einstellungen bequem per Klick ändern, ohne Befehle zu tippen")
    async def panel(self, interaction: discord.Interaction):
        view = SettingsPanel(self, interaction.guild)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @app_commands.command(
        name="management-kanal",
        description="Festen Kanal mit dem Klick-Panel anlegen, z. B. unter einer Admin-Kategorie",
    )
    @app_commands.describe(
        kategorie="Kategorie, unter der der Kanal entsteht (übernimmt deren Sichtbarkeit, z. B. nur fürs Team)",
        neu="Vorhandenen Management-Kanal ersetzen, falls schon einer eingerichtet ist",
    )
    async def management_channel(
        self, interaction: discord.Interaction, kategorie: discord.CategoryChannel, neu: bool = False
    ):
        guild = interaction.guild
        if not guild.me.guild_permissions.manage_channels:
            raise UserError("Mir fehlt das Recht „Kanäle verwalten“.")
        existing_id = self.db.get_int(guild.id, "mgmt_channel")
        if existing_id and not neu:
            existing = guild.get_channel(existing_id)
            if existing:
                raise UserError(f"Es gibt schon einen Management-Kanal: {existing.mention}. "
                                "Mit `neu:True` lege ich einen neuen an.")
        channel = await guild.create_text_channel(
            "bot-einstellungen", category=kategorie, reason=f"Management-Kanal eingerichtet durch {interaction.user}"
        )
        view = SettingsPanel(self, guild, persistent=True)
        message = await channel.send(embed=view.embed(), view=view)
        self.bot.add_view(view, message_id=message.id)
        await self.db.set(guild.id, "mgmt_channel", channel.id)
        await self.db.set(guild.id, "mgmt_message", message.id)
        await interaction.response.send_message(
            f"Fertig: {channel.mention} unter {kategorie.mention} angelegt. Das Panel dort bleibt dauerhaft "
            "klickbar, auch nach einem Neustart.", ephemeral=True
        )

    async def register_persistent_views(self) -> None:
        """Bei jedem Start: Management-Panels wieder anklickbar machen (siehe `management-kanal`)."""
        for guild_id in self.db.guilds_with("mgmt_message"):
            guild = self.bot.get_guild(guild_id)
            message_id = self.db.get_int(guild_id, "mgmt_message")
            if guild and message_id:
                self.bot.add_view(SettingsPanel(self, guild, persistent=True), message_id=message_id)

    @app_commands.command(name="log-kanal", description="Kanal für Moderations-Logs (leer lassen = aus)")
    async def log_channel(self, interaction: discord.Interaction, kanal: discord.TextChannel | None = None):
        if kanal and not kanal.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"Ich darf in {kanal.mention} nicht schreiben.", ephemeral=True)
        await self.db.set(interaction.guild_id, "log_channel", kanal and kanal.id)
        await interaction.response.send_message(
            f"Logs gehen jetzt nach {kanal.mention}." if kanal else "Logs sind aus.", ephemeral=True
        )

    @app_commands.command(name="willkommen", description="Begrüßung neuer Mitglieder (Kanal leer lassen = aus)")
    @app_commands.describe(text="Platzhalter: {user} (Erwähnung), {name}, {server}, {anzahl}")
    async def welcome(
        self, interaction: discord.Interaction, kanal: discord.TextChannel | None = None, text: str | None = None
    ):
        await self.db.set(interaction.guild_id, "welcome_channel", kanal and kanal.id)
        if text is not None:
            await self.db.set(interaction.guild_id, "welcome_text", text.replace("\\n", "\n"))
        await interaction.response.send_message(
            f"Begrüßung in {kanal.mention}." if kanal else "Begrüßung ist aus.", ephemeral=True
        )

    @app_commands.command(name="autorolle", description="Rolle, die jedes neue Mitglied bekommt (leer = aus)")
    async def autorole(self, interaction: discord.Interaction, rolle: discord.Role | None = None):
        if rolle and (rolle.managed or rolle >= interaction.guild.me.top_role):
            return await interaction.response.send_message(
                "Diese Rolle kann ich nicht vergeben. Meine Rolle muss in den Servereinstellungen über ihr stehen.",
                ephemeral=True,
            )
        await self.db.set(interaction.guild_id, "autorole", rolle and rolle.id)
        await interaction.response.send_message(
            f"Neue Mitglieder bekommen {rolle.mention}." if rolle else "Autorolle ist aus.", ephemeral=True
        )

    @app_commands.command(name="dj-rolle", description="Wer fremde Songs überspringen/stoppen darf (leer = alle)")
    async def dj_role(self, interaction: discord.Interaction, rolle: discord.Role | None = None):
        await self.db.set(interaction.guild_id, "dj_role", rolle and rolle.id)
        await interaction.response.send_message(
            f"Nur {rolle.mention}, Admins und wer den Song gewünscht hat, dürfen überspringen und stoppen."
            if rolle else "Alle im Sprachkanal dürfen die Musik steuern.",
            ephemeral=True,
        )

    @app_commands.command(name="spamschutz", description="Spamschutz und Einladungslinks einstellen")
    async def antispam(
        self, interaction: discord.Interaction, aktiv: bool, einladungslinks_erlauben: bool | None = None
    ):
        await self.db.set(interaction.guild_id, "antispam", aktiv)
        if einladungslinks_erlauben is not None:
            await self.db.set(interaction.guild_id, "allow_invites", einladungslinks_erlauben)
        await interaction.response.send_message(f"Spamschutz {'an' if aktiv else 'aus'}.", ephemeral=True)

    @app_commands.command(name="verwarnungen", description="Automatische Strafe ab X Verwarnungen (0 = aus)")
    @app_commands.describe(schwelle="Ab wie vielen Verwarnungen die Strafe greift (0 = abschalten)",
                           aktion="Welche Strafe ab der Schwelle")
    async def warn_settings(
        self, interaction: discord.Interaction, schwelle: app_commands.Range[int, 0, 100] = 0,
        aktion: Literal["timeout", "kick", "ban"] = "timeout",
    ):
        if schwelle == 0:
            await self.db.set(interaction.guild_id, "warn_threshold", None)
            await self.db.set(interaction.guild_id, "warn_action", None)
            return await interaction.response.send_message("Automatische Strafe ist aus.", ephemeral=True)
        await self.db.set(interaction.guild_id, "warn_threshold", schwelle)
        await self.db.set(interaction.guild_id, "warn_action", aktion)
        await interaction.response.send_message(
            f"Ab {schwelle} Verwarnungen in Folge: automatisch {aktion}"
            f"{' (1 Std.)' if aktion == 'timeout' else ''}.", ephemeral=True
        )

    @app_commands.command(name="tickets", description="Team-Rolle für Tickets festlegen (sieht neue Tickets, darf schließen)")
    @app_commands.describe(rolle="Rolle, die zu jedem neuen Ticket-Thread hinzugefügt wird")
    async def tickets_settings(self, interaction: discord.Interaction, rolle: discord.Role | None = None):
        await self.db.set(interaction.guild_id, "ticket_team_role", rolle and rolle.id)
        await interaction.response.send_message(
            f"Team-Rolle für Tickets: {rolle.mention}. Panel mit `/ticket-panel` posten."
            if rolle else "Team-Rolle entfernt.", ephemeral=True
        )

    @app_commands.command(name="temp-voice", description="„Join to create“-Kanal anlegen oder festlegen")
    @app_commands.describe(kanal="Vorhandener Sprachkanal; leer lassen = neuen anlegen", aus="Funktion abschalten")
    async def tempvoice(
        self, interaction: discord.Interaction, kanal: discord.VoiceChannel | None = None, aus: bool = False
    ):
        if aus:
            await self.db.set(interaction.guild_id, "tempvoice_hub", None)
            return await interaction.response.send_message("Temp-Voice ist aus.", ephemeral=True)
        if kanal is None:
            perms = interaction.guild.me.guild_permissions
            if not perms.manage_channels:
                return await interaction.response.send_message("Mir fehlt das Recht „Kanäle verwalten“.", ephemeral=True)
            category = await interaction.guild.create_category("Sprachkanäle")
            kanal = await interaction.guild.create_voice_channel("➕ Kanal erstellen", category=category)
        await self.db.set(interaction.guild_id, "tempvoice_hub", kanal.id)
        await interaction.response.send_message(
            f"Wer {kanal.mention} betritt, bekommt einen eigenen Sprachkanal in derselben Kategorie.", ephemeral=True
        )

    @app_commands.command(
        name="verifizierung",
        description="Regeln-Panel: Mitglieder sehen den Server erst nach Bestätigen (mit Vorschau)",
    )
    @app_commands.describe(
        regel_kanal="Kanal mit den Regeln, bleibt für alle sichtbar, auch unverifiziert",
        rolle="Rolle für Verifizierte; leer lassen = neue Rolle „Verifiziert“ anlegen",
        text="Text im Panel (leer lassen = Standardtext)",
        aus="Verifizierung wieder abschalten – alle sehen den Server wieder wie vorher",
    )
    async def verification(
        self, interaction: discord.Interaction, regel_kanal: discord.TextChannel | None = None,
        rolle: discord.Role | None = None, text: str | None = None, aus: bool = False,
    ):
        guild = interaction.guild
        me = guild.me

        if aus:
            if not self.db.get_bool(guild.id, "verify_active"):
                return await interaction.response.send_message("Verifizierung ist schon aus.", ephemeral=True)
            everyone_perms = guild.default_role.permissions
            everyone_perms.update(view_channel=True)
            await guild.default_role.edit(permissions=everyone_perms, reason=f"Verifizierung aus durch {interaction.user}")
            await self.db.set(guild.id, "verify_active", False)
            return await interaction.response.send_message(
                "Verifizierung ist aus, alle sehen den Server wieder wie vorher. Die Rolle selbst habe ich "
                "nicht gelöscht, das kannst du bei Bedarf in den Servereinstellungen tun.", ephemeral=True,
            )

        if not regel_kanal:
            raise UserError("Gib einen Regel-Kanal an (`regel_kanal:`).")
        if not (me.guild_permissions.manage_roles and me.guild_permissions.manage_channels):
            raise UserError("Mir fehlen die Rechte „Rollen verwalten“ und „Kanäle verwalten“.")
        if rolle and (rolle.managed or rolle >= me.top_role):
            raise UserError(f"{rolle.mention} kann ich nicht vergeben. Meine Rolle muss in den "
                            "Servereinstellungen über ihr stehen.")

        affected = sum(1 for m in guild.members if not m.bot and (not rolle or rolle not in m.roles))
        e = discord.Embed(title="🔒 Verifizierung einrichten?", colour=discord.Colour.orange())
        e.add_field(name="Regel-Kanal", value=regel_kanal.mention, inline=False)
        e.add_field(name="Rolle", value=rolle.mention if rolle else "neu: „Verifiziert“", inline=False)
        e.add_field(
            name="Was passiert",
            value=(f"• @everyone verliert „Kanäle ansehen“ serverweit\n"
                   f"• {regel_kanal.mention} bleibt für alle sichtbar\n"
                   f"• Die Verifiziert-Rolle bekommt „Kanäle ansehen“ automatisch zurück\n"
                   f"• {affected} bestehende Mitglieder bekommen die Rolle sofort, damit niemand ausgesperrt wird\n"
                   f"• Neue Mitglieder bekommen die Autorolle (falls gesetzt) erst nach Klick auf "
                   "„Regeln akzeptiert“"),
            inline=False,
        )
        view = Confirm(interaction.user.id, "Einrichten")
        await interaction.response.send_message(embed=e, view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            return

        if rolle is None:
            rolle = await guild.create_role(name="Verifiziert", reason=f"Verifizierung durch {interaction.user}")
        role_perms = rolle.permissions
        role_perms.update(view_channel=True)
        await rolle.edit(permissions=role_perms, reason="Verifizierung eingerichtet")

        for member in guild.members:
            if not member.bot and rolle not in member.roles:
                try:
                    await member.add_roles(rolle, reason="Verifizierung eingerichtet – Bestandsmitglied")
                except discord.HTTPException as ex:
                    log.warning("Verifizierung Bestandsrolle %s: %r", member.id, ex)

        await regel_kanal.set_permissions(
            guild.default_role, view_channel=True, reason="Verifizierung: Regel-Kanal bleibt sichtbar"
        )
        everyone_perms = guild.default_role.permissions
        everyone_perms.update(view_channel=False)
        await guild.default_role.edit(permissions=everyone_perms, reason=f"Verifizierung durch {interaction.user}")

        await self.db.set(guild.id, "verify_role", rolle.id)
        await self.db.set(guild.id, "verify_channel", regel_kanal.id)
        await self.db.set(guild.id, "verify_active", True)

        panel_embed = discord.Embed(
            title="📜 Regeln", description=(text or DEFAULT_VERIFY_TEXT).replace("\\n", "\n"),
            colour=discord.Colour.blurple(),
        )
        await regel_kanal.send(embed=panel_embed, view=VerifyPanelView())
        await interaction.edit_original_response(
            content=f"✅ Verifizierung eingerichtet. Panel steht in {regel_kanal.mention}.", embed=None, view=None
        )

    @app_commands.command(name="dps-webhook", description="Kanal für DeckBar-Gruppen-DPS festlegen")
    @app_commands.describe(kanal="Kanal, in den die Gruppen-DPS nach jedem Kampf gepostet wird")
    async def dps_webhook_cmd(self, interaction: discord.Interaction, kanal: discord.TextChannel):
        token = await ensure_webhook(self.db, interaction.guild_id, kanal.id)
        await interaction.response.send_message(
            f"Gruppen-DPS geht jetzt nach {kanal.mention}.\n\nURL für die DeckBar-Einstellungen:\n"
            f"`{webhook_url(token)}`\n\nGeheim halten — wer die URL hat, kann hier posten.", ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component:
            return
        if (interaction.data or {}).get("custom_id") != "verify:accept" or not interaction.guild:
            return
        guild = interaction.guild
        role_id = self.db.get_int(guild.id, "verify_role")
        role = guild.get_role(role_id) if role_id else None
        if not role:
            return await interaction.response.send_message("Verifizierung ist nicht mehr eingerichtet.", ephemeral=True)
        member = interaction.user
        if role in member.roles:
            return await interaction.response.send_message("Du bist schon verifiziert.", ephemeral=True)
        try:
            await member.add_roles(role, reason="Verifizierung bestätigt")
        except discord.HTTPException:
            return await interaction.response.send_message(
                "Das hat nicht geklappt, sag einem Admin Bescheid.", ephemeral=True
            )
        autorole_id = self.db.get_int(guild.id, "autorole")
        if autorole_id:
            autorole = guild.get_role(autorole_id)
            if autorole and not autorole.managed and autorole < guild.me.top_role and autorole not in member.roles:
                try:
                    await member.add_roles(autorole, reason="Verifizierung bestätigt – Autorolle")
                except discord.HTTPException as ex:
                    log.warning("Autorolle nach Verifizierung %s: %r", member.id, ex)
        await interaction.response.send_message("✅ Verifiziert! Viel Spaß auf dem Server.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Setup(bot))
