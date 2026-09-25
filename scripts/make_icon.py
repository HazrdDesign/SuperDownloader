"""Generate assets/icon.ico, assets/icon.png and assets/icon.icns in the brand colors from app/theme.py.

A download arrow on a rounded tile (no logo). Re-run after changing the theme colors:
    python scripts/make_icon.py
"""

import sys
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import theme  # noqa: E402

ASSETS = ROOT / "assets"
SIZE = 256


def draw(size: int = SIZE) -> Image.Image:
    s = size / 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tile = ImageColor.getrgb(theme.ACCENT) + (255,)
    white = ImageColor.getrgb(theme.ACCENT_TEXT) + (255,)  # the arrow: text color on the accent
    d.rounded_rectangle([8 * s, 8 * s, 248 * s, 248 * s], radius=52 * s, fill=tile)
    # Arrow shaft and head.
    d.rounded_rectangle([108 * s, 44 * s, 148 * s, 138 * s], radius=10 * s, fill=white)
    d.polygon([(70 * s, 118 * s), (186 * s, 118 * s), (128 * s, 180 * s)], fill=white)
    # Tray.
    d.rounded_rectangle([56 * s, 194 * s, 200 * s, 214 * s], radius=10 * s, fill=white)
    return img


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    big = draw(SIZE)
    big.save(ASSETS / "icon.png")
    big.save(ASSETS / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    draw(1024).save(ASSETS / "icon.icns")  # Mac app icon (Pillow writes every size from 16 to 1024)
    print("wrote", ASSETS / "icon.ico", ASSETS / "icon.png", ASSETS / "icon.icns")


if __name__ == "__main__":
    main()
