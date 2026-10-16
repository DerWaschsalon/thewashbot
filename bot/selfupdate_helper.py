"""Einmal-Helfer für `/server-update`: läuft als eigener, kurzlebiger Container (selbes Image,
gestartet von `cogs/server.py`), wartet bis der alte Bot-Container gestoppt ist, entfernt ihn und
legt den neuen mit identischer Konfiguration (Umgebungsvariablen, Volumes, Ports, Restart-Policy,
Netzwerk) aber dem frisch gezogenen Image an.

Läuft bewusst als eigener Container, nicht als Funktion im Bot-Prozess selbst: Der Bot-Prozess
stoppt sich im Zuge des Updates ja gerade selbst — ein Prozess kann sich nicht mitten im eigenen
Lauf durch einen neuen ersetzen. Start: `python -m bot.selfupdate_helper`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

import docker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s selfupdate: %(message)s")
log = logging.getLogger("selfupdate")

TIMEOUT_SECONDS = 60
POLL_SECONDS = 1


def wait_until_stopped(client: docker.DockerClient, name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            container = client.containers.get(name)
        except docker.errors.NotFound:
            return True
        if container.status in ("exited", "dead"):
            return True
        time.sleep(POLL_SECONDS)
    return False


def main() -> int:
    old_name = os.environ["SELFUPDATE_OLD_NAME"]
    cfg = json.loads(os.environ["SELFUPDATE_CONFIG"])

    client = docker.from_env()
    low = client.api

    log.info("Warte, bis %s gestoppt ist …", old_name)
    if not wait_until_stopped(client, old_name, TIMEOUT_SECONDS):
        log.error("%s wurde nicht innerhalb von %ss gestoppt, breche ab.", old_name, TIMEOUT_SECONDS)
        return 1

    try:
        low.remove_container(old_name)
    except docker.errors.NotFound:
        pass

    log.info("Lege %s mit %s neu an …", cfg["name"], cfg["image"])
    host_config = low.create_host_config(**cfg["host_config_kwargs"])
    container = low.create_container(
        image=cfg["image"], name=cfg["name"], environment=cfg["environment"],
        labels=cfg["labels"], host_config=host_config, ports=[tuple(p) for p in cfg["ports"]],
    )
    low.start(container["Id"])
    log.info("%s läuft wieder, mit neuem Image.", cfg["name"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
