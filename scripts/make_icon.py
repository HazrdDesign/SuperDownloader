"""Generate assets/icon.ico and assets/icon.png (a download arrow on a rounded tile).

Run once with Pillow installed: python scripts/make_icon.py. The outputs are committed.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SIZE = 256


def draw(size: int = SIZE) -> Image.Image:
    s = size / 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8 * s, 8 * s, 248 * s, 248 * s], radius=52 * s, fill=(31, 106, 165, 255))
    white = (255, 255, 255, 255)
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
    print("wrote", ASSETS / "icon.ico", ASSETS / "icon.png")


if __name__ == "__main__":
    main()
