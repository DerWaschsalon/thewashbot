"""Reine Hilfsfunktionen für `/server` (siehe `cogs/server.py`) — bewusst ohne discord.py- oder
docker-Importe, damit sie auch der eigenständige Selbst-Update-Helfer (`selfupdate_helper.py`)
nutzen kann, ohne den ganzen Bot mitzuimportieren, und damit sie sich ohne laufenden Docker-Daemon
testen lassen.
"""

from __future__ import annotations


def cpu_percent_from_samples(a: list[int], b: list[int]) -> float:
    """`a`/`b`: zwei `/proc/stat`-CPU-Zeilen (user nice system idle iowait irq softirq steal …),
    im Abstand einer kurzen Pause gelesen."""
    idle_a, idle_b = a[3] + a[4], b[3] + b[4]
    total_a, total_b = sum(a), sum(b)
    delta_total = total_b - total_a
    delta_idle = idle_b - idle_a
    if delta_total <= 0:
        return 0.0
    return max(0.0, min(100.0, (1 - delta_idle / delta_total) * 100))


def parse_meminfo(text: str) -> dict[str, int]:
    """`/proc/meminfo`-Inhalt -> {Schlüssel: Wert in kB}."""
    info = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.strip().split()
        if parts and parts[0].isdigit():
            info[key] = int(parts[0])
    return info


def ram_usage_kb(info: dict[str, int]) -> tuple[int, int]:
    """(benutzt, gesamt) in kB. „Benutzt" über `MemAvailable` statt `MemFree`, weil Linux
    Datei-Caches sonst fälschlich als „belegt" zählen würde."""
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", info.get("MemFree", 0))
    return max(0, total - available), total


def fmt_bytes(n: float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_uptime(seconds: float) -> str:
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} Tag(e)")
    if hours:
        parts.append(f"{hours} Std.")
    if not days:
        parts.append(f"{minutes} Min.")
    return " ".join(parts) or "0 Min."


def simplify_port_bindings(bindings: dict | None) -> dict:
    """Docker-Inspect-Format `{'8787/tcp': [{'HostIp': '', 'HostPort': '8787'}]}` ->
    `{'8787/tcp': '8787'}`, wie es `APIClient.create_host_config(port_bindings=...)` erwartet."""
    out = {}
    for container_port, host_entries in (bindings or {}).items():
        if host_entries:
            out[container_port] = host_entries[0].get("HostPort")
    return out


def build_recreate_config(inspect_data: dict, new_image: str) -> dict:
    """Übersetzt die Live-Inspect-Daten eines laufenden Containers in die Parameter, um ihn mit
    identischer Konfiguration (Umgebungsvariablen, Volumes, Ports, Restart-Policy, Netzwerk) neu
    anzulegen — nur das Image wird ausgetauscht. Reine Übersetzung ohne Docker-Zugriff, damit sie
    sich ohne laufenden Daemon testen lässt; `self_update_sync` in `cogs/server.py` nutzt das
    Ergebnis, um den Helfer-Container (`selfupdate_helper.py`) mit allem Nötigen zu starten."""
    config = inspect_data.get("Config") or {}
    host_config = inspect_data.get("HostConfig") or {}
    return {
        "image": new_image,
        "name": inspect_data["Name"].lstrip("/"),
        "environment": config.get("Env") or [],
        "labels": config.get("Labels") or {},
        "ports": [p.split("/") for p in (config.get("ExposedPorts") or {})],
        "host_config_kwargs": {
            "binds": host_config.get("Binds") or [],
            "port_bindings": simplify_port_bindings(host_config.get("PortBindings")),
            "restart_policy": host_config.get("RestartPolicy") or {"Name": "unless-stopped"},
            "network_mode": host_config.get("NetworkMode") or "default",
        },
    }
