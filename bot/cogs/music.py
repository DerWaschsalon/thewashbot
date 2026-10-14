"""Musik: Warteschlange pro Server, Steuerung per Befehl oder Buttons an der „Läuft gerade“-Nachricht.

Damit der Kanal sauber bleibt, gibt es pro Server nur eine „Läuft gerade“-Nachricht: Beim nächsten
Song wird die alte gelöscht. Allein im Sprachkanal verlässt der Bot ihn nach einer Minute, bei
leerer Warteschlange nach fünf.
"""

from __future__ import annotations

import asyncio
import logging
import random
import shlex

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ..errors import UserError
from ..sources import RadioBrowser, Track, resolve, stream_url

log = logging.getLogger(__name__)

IDLE_TIMEOUT = 300   # leere Warteschlange → Kanal verlassen
ALONE_TIMEOUT = 60   # niemand mehr im Kanal → verlassen
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
LOOP_LABELS = {"aus": "aus", "song": "Song", "liste": "Warteschlange"}


def fmt_time(seconds: int | None) -> str:
    if not seconds:
        return "?"
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def track_link(t: Track, limit: int = 80) -> str:
    title = discord.utils.escape_markdown(t.title[:limit])
    return f"[{title}]({t.page_url})" if t.page_url else title


class Player:
    """Warteschlange und Abspiel-Schleife eines Servers."""

    def __init__(self, cog: "Music", guild: discord.Guild):
        self.cog = cog
        self.guild = guild
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.loop = "aus"
        self.volume = 0.6
        self.text_channel: discord.abc.Messageable | None = None
        self.np_message: discord.Message | None = None
        self._wakeup = asyncio.Event()
        self._skipped = False
        self.task = asyncio.create_task(self._run(), name=f"player-{guild.id}")

    # ---- Schleife ----

    def _next(self) -> Track | None:
        prev, skipped = self.current, self._skipped
        self._skipped = False
        if prev and not skipped and self.loop == "song":
            return prev
        if prev and self.loop == "liste":
            self.queue.append(prev)
        return self.queue.pop(0) if self.queue else None

    async def _run(self) -> None:
        try:
            while True:
                track = self._next()
                if track is None:
                    self.current = None
                    await self._delete_np()
                    self._wakeup.clear()
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), IDLE_TIMEOUT)
                    except asyncio.TimeoutError:
                        await self.cog.disconnect(self.guild)
                        return
                    continue

                vc = self.guild.voice_client
                if not isinstance(vc, discord.VoiceClient) or not vc.is_connected():
                    return
                self.current = track
                try:
                    url = await stream_url(track)
                except Exception as e:
                    log.warning("Track %r: %r", track.query, e)
                    await self._say(f"⚠️ **{discord.utils.escape_markdown(track.title)}** lässt sich nicht abspielen: {e}")
                    self.current = None
                    continue

                before = FFMPEG_BEFORE
                if track.headers:
                    header_str = "".join(f"{k}: {v}\r\n" for k, v in track.headers.items())
                    before += " -headers " + shlex.quote(header_str)
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(url, before_options=before, options="-vn"), volume=self.volume
                )
                done = asyncio.Event()
                loop = asyncio.get_running_loop()

                def after(err: Exception | None):
                    if err:
                        log.warning("Wiedergabefehler: %r", err)
                    loop.call_soon_threadsafe(done.set)

                vc.play(source, after=after)
                await self._send_np()
                await done.wait()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Player %s abgestürzt", self.guild.id)
            await self.cog.disconnect(self.guild)

    def wake(self) -> None:
        self._wakeup.set()

    def skip(self) -> None:
        self._skipped = True
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    # ---- Nachrichten ----

    async def _say(self, text: str) -> None:
        if self.text_channel:
            try:
                await self.text_channel.send(text, delete_after=30)
            except discord.HTTPException:
                pass

    def np_embed(self) -> discord.Embed:
        t = self.current
        if not t:
            return discord.Embed(description="Gerade läuft nichts.", colour=discord.Colour.dark_grey())
        e = discord.Embed(title="📻 Radio" if t.direct else "🎵 Läuft gerade",
                          description=f"**{track_link(t, 200)}**", colour=discord.Colour.blurple())
        e.add_field(name="Länge", value="Live" if t.live else fmt_time(t.duration))
        e.add_field(name="Gewünscht von", value=f"<@{t.requester_id}>")
        e.add_field(name="Wiederholen", value=LOOP_LABELS[self.loop])
        if self.queue:
            e.add_field(name=f"Als Nächstes ({len(self.queue)} in der Schlange)",
                        value=track_link(self.queue[0]), inline=False)
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        e.set_footer(text=f"Lautstärke {round(self.volume * 100)} %")
        return e

    async def _send_np(self) -> None:
        await self._delete_np()
        if self.text_channel:
            try:
                self.np_message = await self.text_channel.send(embed=self.np_embed(), view=PlayerView(self.cog))
            except discord.HTTPException:
                self.np_message = None

    async def refresh_np(self) -> None:
        if self.np_message:
            try:
                await self.np_message.edit(embed=self.np_embed())
            except discord.HTTPException:
                self.np_message = None

    async def _delete_np(self) -> None:
        if self.np_message:
            try:
                await self.np_message.delete()
            except discord.HTTPException:
                pass
            self.np_message = None


class PlayerView(discord.ui.View):
    """Buttons unter „Läuft gerade“. Gleiche Rechteprüfung wie bei den Befehlen."""

    def __init__(self, cog: "Music"):
        super().__init__(timeout=None)
        self.cog = cog

    async def _player(self, interaction: discord.Interaction) -> Player | None:
        player = self.cog.players.get(interaction.guild_id)
        if not player:
            await interaction.response.send_message("Gerade läuft nichts.", ephemeral=True)
            return None
        if not self.cog.in_bot_channel(interaction.user):
            await interaction.response.send_message("Komm erst in meinen Sprachkanal.", ephemeral=True)
            return None
        return player

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary)
    async def pause(self, interaction: discord.Interaction, _):
        if not await self._player(interaction):
            return
        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
        elif vc.is_playing():
            vc.pause()
        await interaction.response.defer()

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _):
        player = await self._player(interaction)
        if not player:
            return
        if not self.cog.may_control(interaction.user, player):
            return await interaction.response.send_message("Nur die DJ-Rolle oder wer den Song gewünscht hat.", ephemeral=True)
        player.skip()
        await interaction.response.defer()

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, _):
        player = await self._player(interaction)
        if not player:
            return
        if not self.cog.may_control(interaction.user, player):
            return await interaction.response.send_message("Nur die DJ-Rolle oder wer den Song gewünscht hat.", ephemeral=True)
        await interaction.response.defer()
        await self.cog.disconnect(interaction.guild)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, _):
        player = await self._player(interaction)
        if not player:
            return
        order = list(LOOP_LABELS)
        player.loop = order[(order.index(player.loop) + 1) % len(order)]
        await interaction.response.edit_message(embed=player.np_embed())

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary)
    async def show_queue(self, interaction: discord.Interaction, _):
        player = self.cog.players.get(interaction.guild_id)
        await interaction.response.send_message(embed=self.cog.queue_embed(player), ephemeral=True)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players: dict[int, Player] = {}
        self.session: aiohttp.ClientSession | None = None
        self.radio: RadioBrowser | None = None
        self._alone_timers: dict[int, asyncio.Task] = {}

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self.radio = RadioBrowser(self.session)

    async def cog_unload(self):
        for guild_id in list(self.players):
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self.disconnect(guild)
        if self.session:
            await self.session.close()

    # ---- Hilfen ----

    def in_bot_channel(self, member: discord.Member) -> bool:
        vc = member.guild.voice_client
        return bool(vc and member.voice and member.voice.channel == vc.channel)

    def may_control(self, member: discord.Member, player: Player) -> bool:
        dj_role = self.bot.db.get_int(member.guild.id, "dj_role")
        if not dj_role or member.guild_permissions.manage_guild:
            return True
        if any(r.id == dj_role for r in member.roles):
            return True
        if player.current and player.current.requester_id == member.id:
            return True
        vc = member.guild.voice_client
        humans = [m for m in vc.channel.members if not m.bot] if vc else []
        return len(humans) <= 1

    def queue_embed(self, player: Player | None) -> discord.Embed:
        e = discord.Embed(title="📜 Warteschlange", colour=discord.Colour.blurple())
        if not player or (not player.current and not player.queue):
            e.description = "Leer. Mit `/play` geht's los."
            return e
        lines = []
        if player.current:
            lines.append(f"▶️ {track_link(player.current)} · {'Live' if player.current.live else fmt_time(player.current.duration)}")
        for i, t in enumerate(player.queue[:15], 1):
            lines.append(f"`{i:>2}.` {track_link(t, 60)} · {fmt_time(t.duration)}")
        if len(player.queue) > 15:
            lines.append(f"… und {len(player.queue) - 15} weitere")
        total = sum(t.duration or 0 for t in player.queue)
        e.description = "\n".join(lines)
        e.set_footer(text=f"{len(player.queue)} Titel · {fmt_time(total)} · Wiederholen: {LOOP_LABELS[player.loop]}")
        return e

    async def _connect(self, interaction: discord.Interaction) -> Player:
        member = interaction.user
        if not member.voice or not member.voice.channel:
            raise UserError("Geh zuerst in einen Sprachkanal.")
        channel = member.voice.channel
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.connect or not perms.speak:
            raise UserError(f"Ich darf in {channel.mention} nicht beitreten oder sprechen.")
        vc = interaction.guild.voice_client
        player = self.players.get(interaction.guild_id)
        if vc is None:
            await channel.connect(self_deaf=True)
        elif vc.channel != channel:
            if player and (player.current or player.queue):
                raise UserError(f"Ich spiele gerade in {vc.channel.mention}.")
            await vc.move_to(channel)
        if not player or player.task.done():
            player = self.players[interaction.guild_id] = Player(self, interaction.guild)
        player.text_channel = interaction.channel
        return player

    def _require_player(self, interaction: discord.Interaction, *, control: bool = False) -> Player:
        player = self.players.get(interaction.guild_id)
        if not player or not interaction.guild.voice_client:
            raise UserError("Gerade läuft nichts.")
        if not self.in_bot_channel(interaction.user):
            raise UserError("Komm erst in meinen Sprachkanal.")
        if control and not self.may_control(interaction.user, player):
            raise UserError("Das dürfen nur die DJ-Rolle, Admins oder wer den Song gewünscht hat.")
        return player

    async def disconnect(self, guild: discord.Guild) -> None:
        player = self.players.pop(guild.id, None)
        if player:
            player.queue.clear()
            player.current = None
            if player.task is not asyncio.current_task():
                player.task.cancel()
            await player._delete_np()
        timer = self._alone_timers.pop(guild.id, None)
        if timer and timer is not asyncio.current_task():
            timer.cancel()
        vc = guild.voice_client
        if vc:
            await vc.disconnect(force=True)

    async def _enqueue(self, interaction: discord.Interaction, tracks: list[Track], name: str | None):
        player = await self._connect(interaction)
        was_idle = player.current is None
        player.queue.extend(tracks)
        player.wake()
        if len(tracks) == 1:
            t = tracks[0]
            pos = "läuft gleich" if was_idle and len(player.queue) == 1 else f"Platz {len(player.queue)}"
            text = f"➕ {track_link(t)} ({pos})"
        else:
            text = f"➕ **{len(tracks)} Titel**" + (f" aus **{discord.utils.escape_markdown(name)}**" if name else "")
        if player.current and player.current.live:
            text += "\n-# Gerade läuft ein Livestream, `/skip` wechselt zur Warteschlange."
        await interaction.followup.send(text, suppress_embeds=True)
        if not was_idle:
            await player.refresh_np()

    # ---- Befehle ----

    @app_commands.command(name="play", description="Song, Playlist oder Suche abspielen (YouTube, SoundCloud, Spotify)")
    @app_commands.describe(suche="Link oder Suchbegriff", vorne="An den Anfang der Warteschlange")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, suche: str, vorne: bool = False):
        if not interaction.user.voice:
            raise UserError("Geh zuerst in einen Sprachkanal.")
        await interaction.response.defer(thinking=True)
        tracks, name = await resolve(suche, interaction.user.id, self.session)
        if vorne:
            player = await self._connect(interaction)
            player.queue[0:0] = tracks
            player.wake()
            await interaction.followup.send(f"⏫ {track_link(tracks[0])} kommt als Nächstes.", suppress_embeds=True)
            await player.refresh_np()
        else:
            await self._enqueue(interaction, tracks, name)

    @app_commands.command(name="radio", description="Internetradio spielen")
    @app_commands.describe(sender="Sendername (Vorschläge erscheinen beim Tippen)", url="Oder direkt eine Stream-Adresse")
    @app_commands.guild_only()
    async def radio(self, interaction: discord.Interaction, sender: str | None = None, url: str | None = None):
        if not sender and not url:
            raise UserError("Gib einen Sender oder eine Stream-Adresse an.")
        if not interaction.user.voice:
            raise UserError("Geh zuerst in einen Sprachkanal.")
        await interaction.response.defer(thinking=True)
        if url:
            if not url.startswith(("http://", "https://")):
                raise UserError("Das ist keine Stream-Adresse.")
            track = Track(title=url.split("/")[2], query=url, requester_id=interaction.user.id, live=True, direct=True)
        else:
            station = await self.radio.station(sender) if len(sender) == 36 and sender.count("-") == 4 else None
            if not station:
                found = await self.radio.search(sender, limit=1)
                station = found[0] if found else None
            if not station:
                raise UserError(f"Keinen Sender „{sender}“ gefunden.")
            asyncio.create_task(self.radio.click(station["stationuuid"]))
            track = RadioBrowser.to_track(station, interaction.user.id)
        player = await self._connect(interaction)
        # Radio ersetzt, was gerade läuft; die Warteschlange bleibt.
        player.queue.insert(0, track)
        player.loop = "aus"
        if player.current:
            player.skip()
        player.wake()
        await interaction.followup.send(f"📻 **{discord.utils.escape_markdown(track.title)}**")

    @radio.autocomplete("sender")
    async def radio_autocomplete(self, interaction: discord.Interaction, current: str):
        if len(current) < 2:
            return []
        try:
            stations = await asyncio.wait_for(self.radio.search(current, limit=15), 2.5)
        except Exception:
            return []
        choices = []
        for st in stations:
            label = st.get("name", "").strip()
            extra = " · ".join(x for x in (st.get("countrycode"), f"{st.get('bitrate')} kbit/s" if st.get("bitrate") else "") if x)
            if extra:
                label = f"{label} ({extra})"
            choices.append(app_commands.Choice(name=label[:100], value=st["stationuuid"]))
        return choices

    @app_commands.command(name="skip", description="Aktuellen Song überspringen")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        player = self._require_player(interaction, control=True)
        player.skip()
        await interaction.response.send_message("⏭️ Übersprungen.", delete_after=10)

    @app_commands.command(name="stop", description="Musik stoppen, Warteschlange leeren, Kanal verlassen")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        self._require_player(interaction, control=True)
        await self.disconnect(interaction.guild)
        await interaction.response.send_message("⏹️ Gestoppt.", delete_after=10)

    @app_commands.command(name="pause", description="Pausieren")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        self._require_player(interaction)
        interaction.guild.voice_client.pause()
        await interaction.response.send_message("⏸️ Pausiert. Weiter mit `/weiter`.", delete_after=10)

    @app_commands.command(name="weiter", description="Pause beenden")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        self._require_player(interaction)
        interaction.guild.voice_client.resume()
        await interaction.response.send_message("▶️ Weiter geht's.", delete_after=10)

    @app_commands.command(name="warteschlange", description="Warteschlange anzeigen")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=self.queue_embed(self.players.get(interaction.guild_id)), ephemeral=True
        )

    @app_commands.command(name="jetzt", description="Was läuft gerade?")
    @app_commands.guild_only()
    async def now(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if not player or not player.current:
            raise UserError("Gerade läuft nichts.")
        await interaction.response.send_message(embed=player.np_embed(), ephemeral=True)

    @app_commands.command(name="lautstaerke", description="Lautstärke für alle (0–150 %)")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, prozent: app_commands.Range[int, 0, 150]):
        player = self._require_player(interaction, control=True)
        player.volume = prozent / 100
        source = interaction.guild.voice_client.source
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = player.volume
        await interaction.response.send_message(f"🔊 {prozent} %", delete_after=10)
        await player.refresh_np()

    @app_commands.command(name="wiederholen", description="Song oder Warteschlange wiederholen")
    @app_commands.choices(modus=[app_commands.Choice(name=v, value=k) for k, v in LOOP_LABELS.items()])
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, modus: app_commands.Choice[str]):
        player = self._require_player(interaction)
        player.loop = modus.value
        await interaction.response.send_message(f"🔁 Wiederholen: {modus.name}", delete_after=10)
        await player.refresh_np()

    @app_commands.command(name="mischen", description="Warteschlange mischen")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        player = self._require_player(interaction, control=True)
        random.shuffle(player.queue)
        await interaction.response.send_message("🔀 Gemischt.", delete_after=10)
        await player.refresh_np()

    @app_commands.command(name="entfernen", description="Titel aus der Warteschlange entfernen")
    @app_commands.describe(platz="Nummer aus /warteschlange")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, platz: app_commands.Range[int, 1, 1000]):
        player = self._require_player(interaction)
        if platz > len(player.queue):
            raise UserError(f"Die Warteschlange hat nur {len(player.queue)} Titel.")
        track = player.queue[platz - 1]
        if not self.may_control(interaction.user, player) and track.requester_id != interaction.user.id:
            raise UserError("Du darfst nur eigene Titel entfernen.")
        player.queue.pop(platz - 1)
        await interaction.response.send_message(f"🗑️ {track_link(track)} entfernt.", suppress_embeds=True, delete_after=10)
        await player.refresh_np()

    # ---- Sprachkanal beobachten ----

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        if member.id == self.bot.user.id and after.channel is None:
            # Rausgeworfen oder Verbindung verloren
            if guild.id in self.players:
                await self.disconnect(guild)
            return
        vc = guild.voice_client
        if not vc:
            return
        humans = [m for m in vc.channel.members if not m.bot]
        timer = self._alone_timers.get(guild.id)
        if not humans and not timer:
            self._alone_timers[guild.id] = asyncio.create_task(self._leave_when_alone(guild))
        elif humans and timer:
            timer.cancel()
            self._alone_timers.pop(guild.id, None)

    async def _leave_when_alone(self, guild: discord.Guild):
        try:
            await asyncio.sleep(ALONE_TIMEOUT)
            self._alone_timers.pop(guild.id, None)
            await self.disconnect(guild)
        except asyncio.CancelledError:
            pass


async def setup(bot):
    await bot.add_cog(Music(bot))
