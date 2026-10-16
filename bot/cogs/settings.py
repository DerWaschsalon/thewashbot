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


# ---- Verifizierung: geteilte Logik zwischen `/setup verifizierung` und dem Klick-Panel ----


def verification_preview_embed(
    guild: discord.Guild, regel_kanal: discord.TextChannel, rolle: discord.Role | None
) -> discord.Embed:
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
               f"• Die Autorolle (falls gesetzt, z. B. „Neuling“) bekommen neue Mitglieder weiterhin sofort "
               "beim Beitritt, unabhängig von der Verifizierung"),
        inline=False,
    )
    return e


async def apply_verification(
    db, guild: discord.Guild, regel_kanal: discord.TextChannel, rolle: discord.Role | None,
    text: str | None, requested_by,
) -> discord.Role:
    me = guild.me
    if not (me.guild_permissions.manage_roles and me.guild_permissions.manage_channels):
        raise UserError("Mir fehlen die Rechte „Rollen verwalten“ und „Kanäle verwalten“.")
    if rolle and (rolle.managed or rolle >= me.top_role):
        raise UserError(f"{rolle.mention} kann ich nicht vergeben. Meine Rolle muss in den "
                        "Servereinstellungen über ihr stehen.")

    if rolle is None:
        rolle = await guild.create_role(name="Verifiziert", reason=f"Verifizierung durch {requested_by}")
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
    await guild.default_role.edit(permissions=everyone_perms, reason=f"Verifizierung durch {requested_by}")

    await db.set(guild.id, "verify_role", rolle.id)
    await db.set(guild.id, "verify_channel", regel_kanal.id)
    await db.set(guild.id, "verify_active", True)

    panel_embed = discord.Embed(
        title="📜 Regeln", description=(text or DEFAULT_VERIFY_TEXT).replace("\\n", "\n"),
        colour=discord.Colour.blurple(),
    )
    panel_message = await regel_kanal.send(embed=panel_embed, view=VerifyPanelView())
    await db.set(guild.id, "verify_message", panel_message.id)
    return rolle


async def turn_off_verification(db, guild: discord.Guild, requested_by) -> None:
    everyone_perms = guild.default_role.permissions
    everyone_perms.update(view_channel=True)
    await guild.default_role.edit(permissions=everyone_perms, reason=f"Verifizierung aus durch {requested_by}")
    await db.set(guild.id, "verify_active", False)


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
        super().__init__(placeholder="Team-Rolle(n) für Tickets wählen … (ersetzt die bisherige Auswahl)",
                         min_values=1, max_values=10, row=0, custom_id="setup:select:ticket_team_role")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        ids = ",".join(str(r.id) for r in self.values)
        await self.panel.cog.db.set(interaction.guild_id, "ticket_team_roles", ids)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class TicketTeamRoleView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(TicketTeamRoleSelect(panel))
        self.add_item(ClearButton("Team-Rollen aus", "ticket_team_roles", panel))
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


class AionSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Kanal für AION-2-Duplikatfilter wählen …", channel_types=[discord.ChannelType.text],
                         row=0, custom_id="setup:select:aion")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        await self.panel.cog.db.set(interaction.guild_id, "aion_channel", channel.id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=AionView(self.panel))
        await interaction.followup.send(
            f"AION-2-Duplikatfilter läuft jetzt für {channel.mention}.", ephemeral=True,
        )


class AionTokenButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="API-Token erzeugen/anzeigen", emoji="🔑", style=discord.ButtonStyle.secondary,
                         row=1, custom_id="setup:aion_token")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        import secrets

        db = self.panel.cog.db
        token = db.get(interaction.guild_id, "aion_api_token")
        if not token:
            token = secrets.token_urlsafe(24)
            await db.set(interaction.guild_id, "aion_api_token", token)
        aion_cog = self.panel.cog.bot.get_cog("AionDedup")
        await interaction.response.send_message(
            f"API-Variante (braucht eine DeckBar-Änderung):\n`POST {aion_cog.api_url()}`\n"
            f"`Authorization: Bearer {token}`\n\nGeheim halten. Für einen neuen Token (macht den "
            "alten ungültig): `/aion token:True`.",
            ephemeral=True,
        )


class AionOffButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="AION-Filter aus", emoji="✖️", style=discord.ButtonStyle.danger,
                         row=1, custom_id="setup:aion_off")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await self.panel.cog.db.set(interaction.guild_id, "aion_channel", None)
        await interaction.response.edit_message(embed=self.panel.embed(), view=AionView(self.panel))


class AionView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(AionSelect(panel))
        self.add_item(AionTokenButton(panel))
        self.add_item(AionOffButton(panel))
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


class WarnThresholdModal(discord.ui.Modal, title="Verwarnungs-Schwelle"):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__()
        self.panel = panel
        current = panel.cog.db.get_int(panel.guild.id, "warn_threshold") or 0
        self.schwelle = discord.ui.TextInput(label="Schwelle (0 = aus)", default=str(current), max_length=3)
        self.add_item(self.schwelle)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.schwelle.value).strip()
        if not raw.isdigit() or not (0 <= int(raw) <= 100):
            return await interaction.response.send_message("Bitte eine Zahl von 0 bis 100 eingeben.", ephemeral=True)
        value = int(raw)
        db = self.panel.cog.db
        if value == 0:
            await db.set(interaction.guild_id, "warn_threshold", None)
            await db.set(interaction.guild_id, "warn_action", None)
        else:
            await db.set(interaction.guild_id, "warn_threshold", value)
            if not db.get(interaction.guild_id, "warn_action"):
                await db.set(interaction.guild_id, "warn_action", "timeout")
        await interaction.response.edit_message(embed=self.panel.embed(), view=WarnView(self.panel))


class WarnThresholdButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="Schwelle ändern", emoji="🔢", style=discord.ButtonStyle.secondary, row=0,
                         custom_id="setup:warn_threshold_btn")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(WarnThresholdModal(self.panel))


class WarnActionButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel", action: str, label: str):
        current = panel.cog.db.get(panel.guild.id, "warn_action")
        super().__init__(
            label=label, style=discord.ButtonStyle.success if current == action else discord.ButtonStyle.secondary,
            row=1, custom_id=f"setup:warn_action:{action}",
        )
        self.panel = panel
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        db = self.panel.cog.db
        threshold = db.get_int(interaction.guild_id, "warn_threshold") or 3
        await db.set(interaction.guild_id, "warn_threshold", threshold)
        await db.set(interaction.guild_id, "warn_action", self.action)
        await interaction.response.edit_message(embed=self.panel.embed(), view=WarnView(self.panel))


class WarnOffButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="Automatische Strafe aus", emoji="🚫", style=discord.ButtonStyle.danger, row=2,
                         custom_id="setup:warn_off")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        db = self.panel.cog.db
        await db.set(interaction.guild_id, "warn_threshold", None)
        await db.set(interaction.guild_id, "warn_action", None)
        await interaction.response.edit_message(embed=self.panel.embed(), view=WarnView(self.panel))


class WarnView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(WarnThresholdButton(panel))
        self.add_item(WarnActionButton(panel, "timeout", "Timeout (1 Std.)"))
        self.add_item(WarnActionButton(panel, "kick", "Kick"))
        self.add_item(WarnActionButton(panel, "ban", "Ban"))
        self.add_item(WarnOffButton(panel))
        self.add_item(BackButton(panel))


class DashboardRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Rolle mit Dashboard-Zugriff wählen …", row=0,
                         custom_id="setup:select:dashboard_role")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await self.panel.cog.db.set(interaction.guild_id, "dashboard_admin_role", self.values[0].id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class DashboardRoleView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(DashboardRoleSelect(panel))
        self.add_item(ClearButton("Nur „Server verwalten“", "dashboard_admin_role", panel))
        self.add_item(BackButton(panel))


class BumpChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Kanal wählen, in dem ihr /bump bei Disboard nutzt …",
                         channel_types=[discord.ChannelType.text], row=0, custom_id="setup:select:bump_channel")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.values[0].id) or self.values[0]
        await self.panel.cog.db.set(interaction.guild_id, "bump_channel", channel.id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=BumpView(self.panel))


class BumpRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Rolle für die Erinnerung wählen … (optional)", row=1,
                         custom_id="setup:select:bump_role")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await self.panel.cog.db.set(interaction.guild_id, "bump_role", self.values[0].id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=BumpView(self.panel))


class BumpView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(BumpChannelSelect(panel))
        self.add_item(BumpRoleSelect(panel))
        self.add_item(ClearButton("Bump-Kanal aus", "bump_channel", panel, row=2))
        self.add_item(ClearButton("Bump-Rolle aus", "bump_role", panel, row=2))
        self.add_item(BackButton(panel))


class MemberCountChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Sprachkanal wählen, der die Mitgliederzahl im Namen zeigt …",
                         channel_types=[discord.ChannelType.voice], row=0,
                         custom_id="setup:select:member_count_channel")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.values[0].id) or self.values[0]
        if hasattr(channel, "permissions_for") and not channel.permissions_for(interaction.guild.me).manage_channels:
            return await interaction.response.send_message("Mir fehlt das Recht „Kanäle verwalten“.", ephemeral=True)
        await self.panel.cog.db.set(interaction.guild_id, "member_count_channel", channel.id)
        stats_cog = self.panel.cog.bot.get_cog("Stats")
        if stats_cog:
            await stats_cog.update_guild(interaction.guild)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class BotsCountChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Sprachkanal wählen, der die Bot-Anzahl im Namen zeigt …",
                         channel_types=[discord.ChannelType.voice], row=1,
                         custom_id="setup:select:bots_count_channel")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.values[0].id) or self.values[0]
        if hasattr(channel, "permissions_for") and not channel.permissions_for(interaction.guild.me).manage_channels:
            return await interaction.response.send_message("Mir fehlt das Recht „Kanäle verwalten“.", ephemeral=True)
        await self.panel.cog.db.set(interaction.guild_id, "bots_count_channel", channel.id)
        stats_cog = self.panel.cog.bot.get_cog("Stats")
        if stats_cog:
            await stats_cog.update_guild(interaction.guild)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class MemberCountView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(MemberCountChannelSelect(panel))
        self.add_item(BotsCountChannelSelect(panel))
        self.add_item(ClearButton("Mitglieder-Zähler aus", "member_count_channel", panel, row=2))
        self.add_item(ClearButton("Bots-Zähler aus", "bots_count_channel", panel, row=2))
        self.add_item(BackButton(panel))


class StarboardChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Highlight-Kanal für das Starboard wählen …",
                         channel_types=[discord.ChannelType.text], row=0, custom_id="setup:select:starboard_channel")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.values[0].id) or self.values[0]
        if hasattr(channel, "permissions_for") and not channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"Ich darf in {channel.mention} nicht schreiben.", ephemeral=True)
        await self.panel.cog.db.set(interaction.guild_id, "starboard_channel", channel.id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=StarboardView(self.panel))


class StarboardThresholdModal(discord.ui.Modal, title="Starboard-Schwelle"):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__()
        self.panel = panel
        current = panel.cog.db.get_int(panel.guild.id, "starboard_threshold") or 3
        self.schwelle = discord.ui.TextInput(label="Sterne bis zum Highlight-Kanal", default=str(current), max_length=3)
        self.add_item(self.schwelle)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.schwelle.value).strip()
        if not raw.isdigit() or not (1 <= int(raw) <= 100):
            return await interaction.response.send_message("Bitte eine Zahl von 1 bis 100 eingeben.", ephemeral=True)
        await self.panel.cog.db.set(interaction.guild_id, "starboard_threshold", int(raw))
        await interaction.response.edit_message(embed=self.panel.embed(), view=StarboardView(self.panel))


class StarboardThresholdButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="Schwelle ändern", emoji="🔢", style=discord.ButtonStyle.secondary, row=1,
                         custom_id="setup:starboard_threshold_btn")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(StarboardThresholdModal(self.panel))


class StarboardView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(StarboardChannelSelect(panel))
        self.add_item(StarboardThresholdButton(panel))
        self.add_item(ClearButton("Starboard aus", "starboard_channel", panel, row=2))
        self.add_item(BackButton(panel))


class BirthdayChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Kanal für Geburtstagsgrüße wählen …", channel_types=[discord.ChannelType.text],
                         row=0, custom_id="setup:select:birthday_channel")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.values[0].id) or self.values[0]
        if hasattr(channel, "permissions_for") and not channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"Ich darf in {channel.mention} nicht schreiben.", ephemeral=True)
        await self.panel.cog.db.set(interaction.guild_id, "birthday_channel", channel.id)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class BirthdayView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(BirthdayChannelSelect(panel))
        self.add_item(ClearButton("Geburtstagsgrüße aus", "birthday_channel", panel))
        self.add_item(BackButton(panel))


class VoiceXpModal(discord.ui.Modal, title="Sprachzeit-XP"):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__()
        self.panel = panel
        current = panel.cog.db.get_int(panel.guild.id, "voice_xp_per_minute") or 0
        self.xp = discord.ui.TextInput(label="XP pro Minute im Sprachkanal (0 = aus)", default=str(current), max_length=4)
        self.add_item(self.xp)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.xp.value).strip()
        if not raw.isdigit() or not (0 <= int(raw) <= 1000):
            return await interaction.response.send_message("Bitte eine Zahl von 0 bis 1000 eingeben.", ephemeral=True)
        await self.panel.cog.db.set(interaction.guild_id, "voice_xp_per_minute", int(raw) or None)
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class VoiceXpButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="Sprachzeit-XP", emoji="🎙️", style=discord.ButtonStyle.secondary, row=4,
                         custom_id="setup:voice_xp_btn")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VoiceXpModal(self.panel))


class AutomodModal(discord.ui.Modal, title="Automod: Filter"):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__()
        self.panel = panel
        db = panel.cog.db
        self.words = discord.ui.TextInput(
            label="Wortfilter (durch Komma getrennt)", style=discord.TextStyle.paragraph, required=False,
            default=db.get(panel.guild.id, "automod_banned_words") or "",
        )
        self.domains = discord.ui.TextInput(
            label="Erlaubte Link-Domains (durch Komma getrennt)", style=discord.TextStyle.paragraph, required=False,
            default=db.get(panel.guild.id, "automod_allowed_domains") or "",
        )
        self.emoji_limit = discord.ui.TextInput(
            label="Emoji-Grenze pro Nachricht (0 = aus)", required=False, max_length=3,
            default=str(db.get_int(panel.guild.id, "automod_emoji_limit") or 0),
        )
        for item in (self.words, self.domains, self.emoji_limit):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        raw_limit = str(self.emoji_limit.value).strip()
        if raw_limit and not raw_limit.isdigit():
            return await interaction.response.send_message("Emoji-Grenze muss eine Zahl sein.", ephemeral=True)
        db = self.panel.cog.db
        await db.set(interaction.guild_id, "automod_banned_words", str(self.words.value).strip() or None)
        await db.set(interaction.guild_id, "automod_allowed_domains", str(self.domains.value).strip() or None)
        await db.set(interaction.guild_id, "automod_emoji_limit", int(raw_limit) if raw_limit and int(raw_limit) else None)
        await interaction.response.edit_message(embed=self.panel.embed(), view=AutomodView(self.panel))


class AutomodFiltersButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="Wortfilter/Domains/Emoji-Grenze", emoji="✏️", style=discord.ButtonStyle.secondary,
                         row=0, custom_id="setup:automod_filters_btn")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AutomodModal(self.panel))


class AutomodView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.panel = panel
        self.add_item(AutomodFiltersButton(panel))
        caps_on = panel.cog.db.get_bool(panel.guild.id, "automod_caps")
        caps = discord.ui.Button(
            label=f"Großbuchstaben-Filter: {'an' if caps_on else 'aus'} (klicken zum Umschalten)",
            emoji="🔠", style=discord.ButtonStyle.success if caps_on else discord.ButtonStyle.secondary, row=1,
            custom_id="setup:toggle:automod_caps",
        )
        caps.callback = self._toggle_caps
        self.add_item(caps)
        link_on = panel.cog.db.get_bool(panel.guild.id, "automod_link_filter")
        links = discord.ui.Button(
            label=f"Link-Filter (nur erlaubte Domains): {'an' if link_on else 'aus'} (klicken zum Umschalten)",
            emoji="🔗", style=discord.ButtonStyle.success if link_on else discord.ButtonStyle.secondary, row=2,
            custom_id="setup:toggle:automod_link_filter",
        )
        links.callback = self._toggle_links
        self.add_item(links)
        self.add_item(BackButton(panel))

    async def _toggle_caps(self, interaction: discord.Interaction):
        active = self.panel.cog.db.get_bool(interaction.guild_id, "automod_caps")
        await self.panel.cog.db.set(interaction.guild_id, "automod_caps", not active)
        await interaction.response.edit_message(embed=self.panel.embed(), view=AutomodView(self.panel))

    async def _toggle_links(self, interaction: discord.Interaction):
        active = self.panel.cog.db.get_bool(interaction.guild_id, "automod_link_filter")
        await self.panel.cog.db.set(interaction.guild_id, "automod_link_filter", not active)
        await interaction.response.edit_message(embed=self.panel.embed(), view=AutomodView(self.panel))


class VerifySetupChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(placeholder="Regel-Kanal wählen …", channel_types=[discord.ChannelType.text], row=0,
                         custom_id="setup:select:verify_setup_channel")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.values[0].id) or self.values[0]
        await interaction.response.edit_message(view=VerifySetupRoleView(self.panel, channel))


class VerifySetupRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: "SettingsPanel", channel: discord.TextChannel):
        super().__init__(placeholder="Vorhandene Rolle für Verifizierte wählen …", row=0,
                         custom_id="setup:select:verify_setup_role")
        self.panel = panel
        self.channel = channel

    async def callback(self, interaction: discord.Interaction):
        await start_verification_confirm(interaction, self.panel, self.channel, self.values[0])


class VerifySetupNewRoleButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel", channel: discord.TextChannel):
        super().__init__(label="Stattdessen neue Rolle „Verifiziert“ anlegen", style=discord.ButtonStyle.secondary,
                         row=1, custom_id="setup:verify_setup_new_role")
        self.panel = panel
        self.channel = channel

    async def callback(self, interaction: discord.Interaction):
        await start_verification_confirm(interaction, self.panel, self.channel, None)


class VerifySetupRoleView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel", channel: discord.TextChannel):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(VerifySetupRoleSelect(panel, channel))
        self.add_item(VerifySetupNewRoleButton(panel, channel))
        self.add_item(BackButton(panel))


class VerifySetupStartButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="Einrichten starten", emoji="🔒", style=discord.ButtonStyle.success, row=0,
                         custom_id="setup:verify_setup_start")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        me = interaction.guild.me
        if not (me.guild_permissions.manage_roles and me.guild_permissions.manage_channels):
            return await interaction.response.send_message(
                "Mir fehlen die Rechte „Rollen verwalten“ und „Kanäle verwalten“.", ephemeral=True,
            )
        view = discord.ui.View(timeout=None if self.panel.persistent else 180)
        view.add_item(VerifySetupChannelSelect(self.panel))
        view.add_item(BackButton(self.panel))
        await interaction.response.edit_message(view=view)


async def start_verification_confirm(
    interaction: discord.Interaction, panel: "SettingsPanel", channel: discord.TextChannel, rolle: discord.Role | None
) -> None:
    if rolle and (rolle.managed or rolle >= interaction.guild.me.top_role):
        return await interaction.response.send_message(
            f"{rolle.mention} kann ich nicht vergeben. Meine Rolle muss in den Servereinstellungen über ihr stehen.",
            ephemeral=True,
        )
    e = verification_preview_embed(interaction.guild, channel, rolle)
    confirm = Confirm(interaction.user.id, "Einrichten")
    await interaction.response.edit_message(embed=e, view=confirm)
    await confirm.wait()
    if not confirm.confirmed:
        return
    await apply_verification(panel.cog.db, interaction.guild, channel, rolle, None, interaction.user)
    await interaction.edit_original_response(
        content=f"✅ Verifizierung eingerichtet. Panel steht in {channel.mention}.",
        embed=panel.embed(), view=panel,
    )


class VerifyTextModal(discord.ui.Modal, title="Regeln-Text"):
    def __init__(self, panel: "SettingsPanel", current: str):
        super().__init__()
        self.panel = panel
        self.text = discord.ui.TextInput(
            label="Regeln-Text", style=discord.TextStyle.paragraph, default=current, max_length=1000,
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        db = self.panel.cog.db
        channel_id = db.get_int(interaction.guild_id, "verify_channel")
        message_id = db.get_int(interaction.guild_id, "verify_message")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if not channel or not message_id:
            return await interaction.response.send_message("Verifizierung ist nicht mehr eingerichtet.", ephemeral=True)
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return await interaction.response.send_message(
                "Das Regeln-Panel wurde gelöscht. Einmal neu einrichten.", ephemeral=True
            )
        embed = message.embeds[0] if message.embeds else discord.Embed(title="📜 Regeln", colour=discord.Colour.blurple())
        embed.description = str(self.text.value).replace("\\n", "\n")
        await message.edit(embed=embed)
        await interaction.response.edit_message(embed=self.panel.embed(), view=VerifyView(self.panel))


class VerifyTextButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="Text ändern", emoji="✏️", style=discord.ButtonStyle.secondary, row=0,
                         custom_id="setup:verify_text_btn")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        db = self.panel.cog.db
        channel_id = db.get_int(interaction.guild_id, "verify_channel")
        message_id = db.get_int(interaction.guild_id, "verify_message")
        current = DEFAULT_VERIFY_TEXT
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if channel and message_id:
            try:
                message = await channel.fetch_message(message_id)
                if message.embeds and message.embeds[0].description:
                    current = message.embeds[0].description
            except discord.NotFound:
                pass
        await interaction.response.send_modal(VerifyTextModal(self.panel, current))


class VerifyOffButton(discord.ui.Button):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(label="Ausschalten", emoji="🔓", style=discord.ButtonStyle.danger, row=0,
                         custom_id="setup:verify_off")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await turn_off_verification(self.panel.cog.db, interaction.guild, interaction.user)
        await interaction.response.edit_message(embed=self.panel.embed(), view=VerifyView(self.panel))


class VerifyView(discord.ui.View):
    def __init__(self, panel: "SettingsPanel"):
        super().__init__(timeout=None if panel.persistent else 180)
        self.add_item(VerifyTextButton(panel) if panel.cog.db.get_bool(panel.guild.id, "verify_active")
                     else VerifySetupStartButton(panel))
        if panel.cog.db.get_bool(panel.guild.id, "verify_active"):
            self.add_item(VerifyOffButton(panel))
        self.add_item(BackButton(panel))


class SettingsPanel(discord.ui.View):
    """Hauptmenü. `persistent=True`: fest in einem Kanal gepostet, überlebt Neustarts (siehe
    `Setup.register_persistent_views`); sonst normale ephemere Antwort auf `/setup panel`."""

    def __init__(self, cog: "Setup", guild: discord.Guild, *, persistent: bool = False):
        super().__init__(timeout=None if persistent else 180)
        self.cog = cog
        self.guild = guild
        self.persistent = persistent
        self.add_item(VoiceXpButton(self))
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

    @discord.ui.button(label="AION-2-Filter", emoji="🗡️", style=discord.ButtonStyle.secondary, row=2,
                       custom_id="setup:open:aion")
    async def aion(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=AionView(self))

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

    @discord.ui.button(label="Verwarnungen", emoji="⚠️", style=discord.ButtonStyle.secondary, row=3,
                       custom_id="setup:open:warn")
    async def warn_settings(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=WarnView(self))

    @discord.ui.button(label="Dashboard", emoji="🖥️", style=discord.ButtonStyle.secondary, row=3,
                       custom_id="setup:open:dashboard_role")
    async def dashboard_role(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=DashboardRoleView(self))

    @discord.ui.button(label="Verifizierung", emoji="🔒", style=discord.ButtonStyle.secondary, row=3,
                       custom_id="setup:open:verify")
    async def verify(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=VerifyView(self))

    @discord.ui.button(label="Bump-Erinnerung", emoji="📈", style=discord.ButtonStyle.secondary, row=3,
                       custom_id="setup:open:bump")
    async def bump(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=BumpView(self))

    @discord.ui.button(label="Statistik-Kanäle", emoji="👥", style=discord.ButtonStyle.secondary, row=3,
                       custom_id="setup:open:member_count")
    async def member_count(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=MemberCountView(self))

    @discord.ui.button(label="Starboard", emoji="⭐", style=discord.ButtonStyle.secondary, row=4,
                       custom_id="setup:open:starboard")
    async def starboard(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=StarboardView(self))

    @discord.ui.button(label="Geburtstage", emoji="🎂", style=discord.ButtonStyle.secondary, row=4,
                       custom_id="setup:open:birthday")
    async def birthday(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=BirthdayView(self))

    @discord.ui.button(label="Automod", emoji="🧹", style=discord.ButtonStyle.secondary, row=4,
                       custom_id="setup:open:automod")
    async def automod(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(view=AutomodView(self))


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
        e.add_field(name="AION-2-Duplikatfilter", value=fmt_channel(guild, db.get_int(guild.id, "aion_channel")))
        ticket_roles = [fmt_role(guild, i) for i in db.get_ids(guild.id, "ticket_team_roles")]
        e.add_field(name="Ticket-Team-Rollen", value=", ".join(ticket_roles) if ticket_roles else "–")
        schwelle = db.get_int(guild.id, "warn_threshold")
        aktion = db.get(guild.id, "warn_action")
        e.add_field(name="Automatische Strafe", value=f"ab {schwelle} Verwarnungen: {aktion}" if schwelle else "aus")
        dashboard_role = db.get_int(guild.id, "dashboard_admin_role")
        e.add_field(name="Dashboard-Zugriff", value=fmt_role(guild, dashboard_role) if dashboard_role else "nur „Server verwalten“")
        verify_active = db.get_bool(guild.id, "verify_active")
        e.add_field(name="Verifizierung",
                    value=f"an – {fmt_channel(guild, db.get_int(guild.id, 'verify_channel'))}" if verify_active else "aus")
        bump_channel = db.get_int(guild.id, "bump_channel")
        e.add_field(name="Bump-Erinnerung", value=fmt_channel(guild, bump_channel) if bump_channel else "aus")
        member_count_channel = db.get_int(guild.id, "member_count_channel")
        e.add_field(name="Mitglieder-Zähler", value=fmt_channel(guild, member_count_channel) if member_count_channel else "aus")
        bots_count_channel = db.get_int(guild.id, "bots_count_channel")
        e.add_field(name="Bots-Zähler", value=fmt_channel(guild, bots_count_channel) if bots_count_channel else "aus")
        starboard_channel = db.get_int(guild.id, "starboard_channel")
        starboard_threshold = db.get_int(guild.id, "starboard_threshold") or 3
        e.add_field(name="Starboard",
                    value=f"{fmt_channel(guild, starboard_channel)} (ab {starboard_threshold} ⭐)" if starboard_channel else "aus")
        birthday_channel = db.get_int(guild.id, "birthday_channel")
        e.add_field(name="Geburtstagsgrüße", value=fmt_channel(guild, birthday_channel) if birthday_channel else "aus")
        voice_xp = db.get_int(guild.id, "voice_xp_per_minute")
        e.add_field(name="Sprachzeit-XP", value=f"{voice_xp} XP/Min." if voice_xp else "aus")
        automod_bits = []
        if db.get(guild.id, "automod_banned_words"):
            automod_bits.append("Wortfilter")
        if db.get_bool(guild.id, "automod_caps"):
            automod_bits.append("Großbuchstaben")
        if db.get_int(guild.id, "automod_emoji_limit"):
            automod_bits.append("Emoji-Grenze")
        if db.get_bool(guild.id, "automod_link_filter"):
            automod_bits.append("Link-Filter")
        e.add_field(name="Automod", value=", ".join(automod_bits) if automod_bits else "aus")
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

    @app_commands.command(name="tickets", description="Team-Rolle(n) für Tickets hinzufügen/entfernen (mehrere möglich)")
    @app_commands.describe(
        rolle="Rolle, die hinzugefügt wird (oder entfernt, mit entfernen:True) — leer lässt alles wie es ist",
        entfernen="Statt hinzuzufügen die Rolle aus der Liste entfernen",
    )
    async def tickets_settings(
        self, interaction: discord.Interaction, rolle: discord.Role | None = None, entfernen: bool = False
    ):
        ids = self.db.get_ids(interaction.guild_id, "ticket_team_roles")
        if rolle:
            ids = [i for i in ids if i != rolle.id] if entfernen else list(dict.fromkeys(ids + [rolle.id]))
            await self.db.set(interaction.guild_id, "ticket_team_roles", ",".join(map(str, ids)) or None)
        roles = [r for i in ids if (r := interaction.guild.get_role(i))]
        await interaction.response.send_message(
            f"Team-Rollen für Tickets: {', '.join(r.mention for r in roles) if roles else 'keine'}. "
            "Panel mit `/ticket-panel` posten.", ephemeral=True,
        )

    @app_commands.command(name="dashboard", description="Wer darf das Web-Dashboard öffnen (leer lassen = nur „Server verwalten“)")
    @app_commands.describe(rolle="Rolle mit Zugriff aufs Dashboard; leer = Rolle entfernen (Fallback: „Server verwalten“)")
    async def dashboard_settings(self, interaction: discord.Interaction, rolle: discord.Role | None = None):
        await self.db.set(interaction.guild_id, "dashboard_admin_role", rolle and rolle.id)
        await interaction.response.send_message(
            f"Dashboard-Zugriff: {rolle.mention}." if rolle else
            "Dashboard-Zugriff: nur noch Leute mit „Server verwalten“.", ephemeral=True,
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
            await turn_off_verification(self.db, guild, interaction.user)
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

        e = verification_preview_embed(guild, regel_kanal, rolle)
        view = Confirm(interaction.user.id, "Einrichten")
        await interaction.response.send_message(embed=e, view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            return

        await apply_verification(self.db, guild, regel_kanal, rolle, text, interaction.user)
        await interaction.edit_original_response(
            content=f"✅ Verifizierung eingerichtet. Panel steht in {regel_kanal.mention}.", embed=None, view=None
        )

    @app_commands.command(name="regeln-text", description="Text im Regeln-Panel ändern (Verifizierung muss schon eingerichtet sein)")
    @app_commands.describe(text="Neuer Text im Regeln-Panel")
    async def rules_text(self, interaction: discord.Interaction, text: str):
        channel_id = self.db.get_int(interaction.guild_id, "verify_channel")
        message_id = self.db.get_int(interaction.guild_id, "verify_message")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if not channel or not message_id:
            raise UserError("Noch keine Verifizierung eingerichtet: `/setup verifizierung`.")
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            raise UserError("Das Regeln-Panel wurde gelöscht. Einmal neu einrichten: `/setup verifizierung`.")
        embed = message.embeds[0] if message.embeds else discord.Embed(title="📜 Regeln", colour=discord.Colour.blurple())
        embed.description = text.replace("\\n", "\n")
        await message.edit(embed=embed)
        await interaction.response.send_message(f"Regeln-Text in {channel.mention} aktualisiert.", ephemeral=True)

    @app_commands.command(name="dps-webhook", description="Kanal für DeckBar-Gruppen-DPS festlegen")
    @app_commands.describe(kanal="Kanal, in den die Gruppen-DPS nach jedem Kampf gepostet wird")
    async def dps_webhook_cmd(self, interaction: discord.Interaction, kanal: discord.TextChannel):
        token = await ensure_webhook(self.db, interaction.guild_id, kanal.id)
        await interaction.response.send_message(
            f"Gruppen-DPS geht jetzt nach {kanal.mention}.\n\nURL für die DeckBar-Einstellungen:\n"
            f"`{webhook_url(token)}`\n\nGeheim halten — wer die URL hat, kann hier posten.", ephemeral=True,
        )

    @app_commands.command(name="bump", description="Erinnerung, wenn der Disboard-Server-Bump wieder möglich ist (alle 2 Std.)")
    @app_commands.describe(
        kanal="Kanal, in dem ihr /bump bei Disboard nutzt (dort kommt auch die Erinnerung)",
        rolle="Rolle, die bei der Erinnerung gepingt wird (leer lassen = keine)",
        aus="Erinnerung abschalten",
    )
    async def bump_settings(
        self, interaction: discord.Interaction, kanal: discord.TextChannel | None = None,
        rolle: discord.Role | None = None, aus: bool = False,
    ):
        if aus:
            await self.db.set(interaction.guild_id, "bump_channel", None)
            return await interaction.response.send_message("Bump-Erinnerung ist aus.", ephemeral=True)
        if kanal:
            await self.db.set(interaction.guild_id, "bump_channel", kanal.id)
        if rolle:
            await self.db.set(interaction.guild_id, "bump_role", rolle.id)
        channel_id = self.db.get_int(interaction.guild_id, "bump_channel")
        if not channel_id:
            raise UserError("Gib einen Kanal an (`kanal:`).")
        channel = interaction.guild.get_channel(channel_id)
        rolle = interaction.guild.get_role(self.db.get_int(interaction.guild_id, "bump_role") or 0)
        await interaction.response.send_message(
            f"Bump-Erinnerung aktiv für {channel.mention}" + (f", pingt {rolle.mention}" if rolle else "") + ".",
            ephemeral=True,
        )

    @app_commands.command(name="mitglieder-zaehler", description="Sprachkanal, dessen Name die Mitgliederzahl zeigt")
    @app_commands.describe(kanal="Vorhandener Sprachkanal (Name wird z. B. zu „👥 Mitglieder: 123“); leer lassen = aus")
    async def member_count_settings(self, interaction: discord.Interaction, kanal: discord.VoiceChannel | None = None):
        if kanal and not kanal.permissions_for(interaction.guild.me).manage_channels:
            return await interaction.response.send_message("Mir fehlt das Recht „Kanäle verwalten“.", ephemeral=True)
        await self.db.set(interaction.guild_id, "member_count_channel", kanal and kanal.id)
        if kanal:
            stats_cog = self.bot.get_cog("Stats")
            if stats_cog:
                await stats_cog.update_guild(interaction.guild)
        await interaction.response.send_message(
            f"{kanal.mention} zeigt jetzt die Mitgliederzahl." if kanal else "Mitglieder-Zähler ist aus.", ephemeral=True
        )

    @app_commands.command(name="bots-zaehler", description="Sprachkanal, dessen Name die Bot-Anzahl zeigt")
    @app_commands.describe(kanal="Vorhandener Sprachkanal (Name wird z. B. zu „🤖 Bots: 12“); leer lassen = aus")
    async def bots_count_settings(self, interaction: discord.Interaction, kanal: discord.VoiceChannel | None = None):
        if kanal and not kanal.permissions_for(interaction.guild.me).manage_channels:
            return await interaction.response.send_message("Mir fehlt das Recht „Kanäle verwalten“.", ephemeral=True)
        await self.db.set(interaction.guild_id, "bots_count_channel", kanal and kanal.id)
        if kanal:
            stats_cog = self.bot.get_cog("Stats")
            if stats_cog:
                await stats_cog.update_guild(interaction.guild)
        await interaction.response.send_message(
            f"{kanal.mention} zeigt jetzt die Bot-Anzahl." if kanal else "Bots-Zähler ist aus.", ephemeral=True
        )

    @app_commands.command(name="starboard", description="Nachrichten ab X ⭐-Reaktionen in einen Highlight-Kanal spiegeln")
    @app_commands.describe(
        kanal="Highlight-Kanal für gestarrte Nachrichten; leer lassen = aus",
        schwelle="Wie viele ⭐ (ohne die des Autors selbst) nötig sind (Standard 3)",
    )
    async def starboard_settings(
        self, interaction: discord.Interaction, kanal: discord.TextChannel | None = None,
        schwelle: app_commands.Range[int, 1, 100] = 3,
    ):
        if kanal and not kanal.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"Ich darf in {kanal.mention} nicht schreiben.", ephemeral=True)
        await self.db.set(interaction.guild_id, "starboard_channel", kanal and kanal.id)
        if kanal:
            await self.db.set(interaction.guild_id, "starboard_threshold", schwelle)
        await interaction.response.send_message(
            f"Starboard aktiv: ab {schwelle} ⭐ landen Nachrichten in {kanal.mention}." if kanal else "Starboard ist aus.",
            ephemeral=True,
        )

    @app_commands.command(name="geburtstage", description="Kanal für die tägliche Geburtstagsgratulation (9 Uhr)")
    @app_commands.describe(kanal="Kanal für die Gratulation; leer lassen = aus")
    async def birthday_settings(self, interaction: discord.Interaction, kanal: discord.TextChannel | None = None):
        if kanal and not kanal.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"Ich darf in {kanal.mention} nicht schreiben.", ephemeral=True)
        await self.db.set(interaction.guild_id, "birthday_channel", kanal and kanal.id)
        await interaction.response.send_message(
            f"Geburtstagsgrüße gehen jetzt nach {kanal.mention}." if kanal else "Geburtstagsgrüße sind aus.", ephemeral=True
        )

    @app_commands.command(name="sprachzeit-xp", description="XP pro Minute im Sprachkanal (0 = aus)")
    @app_commands.describe(xp="XP pro Minute, die mitzählende Mitglieder im Sprachkanal bekommen")
    async def voice_xp_settings(self, interaction: discord.Interaction, xp: app_commands.Range[int, 0, 1000] = 0):
        await self.db.set(interaction.guild_id, "voice_xp_per_minute", xp or None)
        await interaction.response.send_message(
            f"Sprachzeit-XP: {xp} pro Minute." if xp else "Sprachzeit-XP ist aus.", ephemeral=True
        )

    @app_commands.command(name="automod", description="Wortfilter, Großbuchstaben, Emoji-Spam, Link-Filter einstellen")
    @app_commands.describe(
        wortfilter="Verbotene Wörter, durch Komma getrennt; leer lassen = unverändert",
        grossbuchstaben="Nachrichten mit zu vielen Großbuchstaben entfernen",
        emoji_grenze="Höchstzahl Emojis pro Nachricht (0 = aus); leer lassen = unverändert",
        link_filter="Nur Links zu erlaubten Domains zulassen",
        erlaubte_domains="Erlaubte Domains, durch Komma getrennt (nur mit link_filter:True relevant); leer = unverändert",
    )
    async def automod_settings(
        self, interaction: discord.Interaction, wortfilter: str | None = None,
        grossbuchstaben: bool | None = None, emoji_grenze: app_commands.Range[int, 0, 100] | None = None,
        link_filter: bool | None = None, erlaubte_domains: str | None = None,
    ):
        if wortfilter is not None:
            await self.db.set(interaction.guild_id, "automod_banned_words", wortfilter or None)
        if grossbuchstaben is not None:
            await self.db.set(interaction.guild_id, "automod_caps", grossbuchstaben)
        if emoji_grenze is not None:
            await self.db.set(interaction.guild_id, "automod_emoji_limit", emoji_grenze or None)
        if link_filter is not None:
            await self.db.set(interaction.guild_id, "automod_link_filter", link_filter)
        if erlaubte_domains is not None:
            await self.db.set(interaction.guild_id, "automod_allowed_domains", erlaubte_domains or None)
        await interaction.response.send_message("Automod-Einstellungen aktualisiert.", ephemeral=True)

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
