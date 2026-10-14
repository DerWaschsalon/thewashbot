#!/bin/sh
# YouTube ändert oft etwas, das yt-dlp dann nachzieht. Deshalb bei jedem Start aktualisieren
# (abschalten mit YTDLP_AUTOUPDATE=0).
if [ "${YTDLP_AUTOUPDATE:-1}" = "1" ]; then
    pip install --no-cache-dir --quiet --upgrade "yt-dlp[default]" || echo "yt-dlp-Update fehlgeschlagen, nutze vorhandene Version"
fi
exec python -m bot
