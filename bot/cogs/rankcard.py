"""Rangkarte für /rang: Avatar, Name, Level/Rang, XP-Fortschrittsbalken. Nutzt DejaVu Sans
(Paket `fonts-dejavu-core` im Docker-Image, siehe Dockerfile) — fehlt die Schriftdatei (z. B.
lokal ohne Docker), fällt es auf Pillows eingebaute Mini-Schrift zurück statt zu crashen.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 900, 260
BG_COLOUR = (35, 39, 42)
ACCENT = (88, 101, 242)
TRACK_COLOUR = (60, 64, 68)
TEXT_COLOUR = (255, 255, 255)
MUTED_COLOUR = (170, 170, 170)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

AVATAR_SIZE = 180
MARGIN = 40


def _font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _circle_avatar(data: bytes, size: int) -> Image.Image:
    avatar = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(avatar, (0, 0), mask)
    return out


def render_rank_card(avatar_bytes: bytes, name: str, level: int, rank: int, xp_into_level: int, xp_needed: int) -> io.BytesIO:
    card = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOUR)
    draw = ImageDraw.Draw(card)

    avatar = _circle_avatar(avatar_bytes, AVATAR_SIZE)
    card.paste(avatar, (MARGIN, (HEIGHT - AVATAR_SIZE) // 2), avatar)

    text_x = MARGIN + AVATAR_SIZE + 40
    draw.text((text_x, 50), name, font=_font(FONT_BOLD, 42), fill=TEXT_COLOUR)
    draw.text((text_x, 105), f"Rang #{rank}  •  Level {level}", font=_font(FONT_REGULAR, 28), fill=MUTED_COLOUR)

    bar_y, bar_height = 165, 28
    bar_width = WIDTH - text_x - MARGIN
    draw.rounded_rectangle((text_x, bar_y, text_x + bar_width, bar_y + bar_height), radius=bar_height // 2, fill=TRACK_COLOUR)
    fraction = min(1.0, xp_into_level / xp_needed) if xp_needed else 0.0
    if fraction > 0:
        filled = max(bar_height, int(bar_width * fraction))  # nie schmaler als hoch, sonst verzerrte Rundung
        draw.rounded_rectangle((text_x, bar_y, text_x + filled, bar_y + bar_height), radius=bar_height // 2, fill=ACCENT)
    draw.text((text_x, bar_y + bar_height + 10), f"{xp_into_level} / {xp_needed} XP",
              font=_font(FONT_REGULAR, 22), fill=MUTED_COLOUR)

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    buf.seek(0)
    return buf
