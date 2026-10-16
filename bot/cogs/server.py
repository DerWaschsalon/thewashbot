"""Server-Status (`/server`): CPU/RAM/Uptime/Festplatte, laufende Container, Container neu
starten, Bot-Container per Docker-Socket selbst aktualisieren (`/server-update`). Nur für
`OWNER_ID` (Env) — der Docker-Socket gibt praktisch Root-Zugriff auf den Host, deshalb bewusst auf
eine einzelne Person beschränkt statt auf eine Rolle wie bei den anderen `/setup`-Befehlen.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

import discord
from discord import app_commands
from discord.ext import commands

from .cleanup import Confirm
from ..errors import UserError
from ..serverstatus import build_recreate_config, cpu_percent_from_samples, fmt_bytes, format_uptime, parse_meminfo, ram_usage_kb

log = logging.getLogger(__name__)

SELF_UPDATE_IMAGE = "ghcr.io/navonex/thewashbot:latest"


def require_owner(interaction: discord.Interaction) -> None:
    owner_id = os.getenv("OWNER_ID", "").strip()
    if not owner_id.isdigit() or interaction.user.id != int(owner_id):
        raise UserError("Das geht nur für den Bot-Besitzer (`OWNER_ID` in der `.env`).")


async def read_cpu_percent(sample_seconds: float = 0.3) -> float:
    def _line() -> list[int]:
        with open("/proc/stat") as f:
            return [int(x) for x in f.readline().split()[1:]]

    loop = asyncio.get_running_loop()
    a = await loop.run_in_executor(None, _line)
    await asyncio.sleep(sample_seconds)
    b = await loop.run_in_executor(None, _line)
    return cpu_percent_from_samples(a, b)


def _docker_client():
    import docker

    try:
        return docker.from_env()
    except Exception as ex:
        raise UserError(f"Docker-Socket nicht erreichbar: {ex}")


def self_update_sync(client, container_name: str, image_ref: str) -> None:
    """Läuft in einem Executor-Thread (blockierende Docker-SDK-Aufrufe). Zieht das neue Image,
    liest die laufende Konfiguration des Bot-Containers aus, startet damit den Helfer-Container
    (siehe `selfupdate_helper.py`) und stoppt sich zuletzt selbst — ab da übernimmt der Helfer."""
    import docker

    log.info("Selbst-Update: ziehe %s", image_ref)
    client.images.pull(image_ref)
    inspect_data = client.api.inspect_container(container_name)
    cfg = build_recreate_config(inspect_data, image_ref)

    helper_name = f"{container_name}-selfupdate-helper"
    try:
        client.containers.get(helper_name).remove(force=True)
    except docker.errors.NotFound:
        pass

    client.containers.run(
        image_ref, command=["python", "-m", "bot.selfupdate_helper"], name=helper_name,
        detach=True, remove=True,
        environment={"SELFUPDATE_OLD_NAME": container_name, "SELFUPDATE_CONFIG": json.dumps(cfg)},
        volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
    )
    log.info("Selbst-Update: Helfer gestartet, stoppe mich selbst (%s)", container_name)
    client.api.stop(container_name, timeout=10)


class Server(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="server", description="Server-Status: CPU/RAM/Uptime/Festplatte, laufende Container")
    async def status(self, interaction: discord.Interaction):
        require_owner(interaction)
        await interaction.response.defer(ephemeral=True)
        cpu = await read_cpu_percent()
        with open("/proc/meminfo") as f:
            used_kb, total_kb = ram_usage_kb(parse_meminfo(f.read()))
        with open("/proc/uptime") as f:
            uptime = float(f.readline().split()[0])
        disk = shutil.disk_usage("/data")

        e = discord.Embed(title="🖥️ Server-Status", colour=discord.Colour.blurple())
        e.add_field(name="CPU", value=f"{cpu:.0f}%")
        e.add_field(name="RAM", value=f"{fmt_bytes(used_kb * 1024)} / {fmt_bytes(total_kb * 1024)}")
        e.add_field(name="Uptime", value=format_uptime(uptime))
        e.add_field(name="Festplatte (/data)", value=f"{fmt_bytes(disk.used)} / {fmt_bytes(disk.total)}")

        try:
            client = _docker_client()
            containers = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: [(c.name, c.status, (c.image.tags[0] if c.image.tags else c.image.short_id))
                        for c in client.containers.list(all=True)],
            )
            lines = [f"{'🟢' if status == 'running' else '🔴'} **{name}** ({image}) – {status}"
                    for name, status, image in containers]
            e.add_field(name=f"Container ({len(containers)})", value="\n".join(lines)[:1024] or "–", inline=False)
        except UserError as ex:
            e.add_field(name="Container", value=str(ex), inline=False)

        await interaction.followup.send(embed=e)

    @app_commands.command(name="server-neustart", description="Einen Container neu starten (nicht der Bot selbst)")
    @app_commands.describe(container="Name des Containers, siehe /server")
    async def restart_container(self, interaction: discord.Interaction, container: str):
        require_owner(interaction)
        client = _docker_client()
        confirm = Confirm(interaction.user.id, f"„{container}“ neu starten")
        await interaction.response.send_message(f"**{container}** wirklich neu starten?", view=confirm, ephemeral=True)
        await confirm.wait()
        if not confirm.confirmed:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: client.containers.get(container).restart(timeout=10)
            )
        except Exception as ex:
            return await interaction.followup.send(f"Neustart fehlgeschlagen: {ex}", ephemeral=True)
        await interaction.followup.send(f"**{container}** neu gestartet.", ephemeral=True)

    @app_commands.command(name="server-update", description="Bot-Container mit dem neuesten Image von GHCR aktualisieren")
    async def update_self(self, interaction: discord.Interaction):
        require_owner(interaction)
        container_name = os.getenv("SELF_CONTAINER_NAME", "thewashbot").strip()
        client = _docker_client()
        confirm = Confirm(interaction.user.id, "Jetzt aktualisieren")
        await interaction.response.send_message(
            f"⚠️ Zieht `{SELF_UPDATE_IMAGE}` und startet **{container_name}** damit neu. Der Bot ist dabei "
            "kurz offline. Falls etwas schiefgeht: auf unraid-server `cd /mnt/user/appdata/thewashbot && "
            "docker compose up -d`.", view=confirm, ephemeral=True,
        )
        await confirm.wait()
        if not confirm.confirmed:
            return
        await interaction.followup.send("⏳ Update läuft, der Bot geht gleich kurz offline …", ephemeral=True)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self_update_sync, client, container_name, SELF_UPDATE_IMAGE
            )
        except Exception as ex:
            log.exception("Selbst-Update fehlgeschlagen")
            try:
                await interaction.followup.send(f"Update fehlgeschlagen: {ex}", ephemeral=True)
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(Server(bot))
