"""Channel branding overlays.

The sandbox FFmpeg build has no drawtext filter, so branding is baked into a
PNG (channel name + LIVE chip) once at startup and overlaid by ffmpeg.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .settings import BRAND_ROOT

W, H = 1280, 720


def brand_path(channel_cfg: dict) -> Path:
    return BRAND_ROOT / f"{channel_cfg['id']}.png"


def ensure_brand_png(channel_cfg: dict) -> Path:
    path = brand_path(channel_cfg)
    if path.exists():
        return path
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return path

    def font(size: int):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # top-left channel plate
    name = channel_cfg.get("name", channel_cfg["id"]).upper()
    d.rounded_rectangle((28, 24, 28 + 34 + 13 * len(name), 24 + 58), radius=12,
                        fill=(10, 10, 18, 165))
    d.text((45, 36), name, font=font(34), fill=(245, 245, 252, 255))

    # LIVE chip
    d.rounded_rectangle((28, 96, 28 + 118, 96 + 44), radius=10,
                        fill=(224, 36, 60, 235))
    d.text((48, 104), "LIVE", font=font(26), fill=(255, 255, 255, 255))

    # footer note
    note = "DEMO FEED - REPLACE WITH YOUR OWN LICENSED SOURCES"
    d.rounded_rectangle((28, H - 70, 28 + 30 + 11 * len(note), H - 26), radius=10,
                        fill=(10, 10, 18, 150))
    d.text((44, H - 62), note, font=font(20), fill=(200, 202, 214, 255))

    img.save(path)
    return path
