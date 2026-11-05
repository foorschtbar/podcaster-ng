#!/usr/bin/env python3
"""Generates the PodcasterNG logo, favicons and the Telegram bot picture from one set of shapes.

    python3 branding/make_icons.py          (needs Pillow; run it in a container if you have none)

Writes SVGs and PNGs to server/app/static/ (served by the app) and branding/telegram-bot.png (640x640, upload it to
@BotFather with /setuserpic; Telegram shows it in a circle, the mark stays inside).
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "server" / "app" / "static"
BLUE = "#1d5fd1"
SIZE = 512

# A waveform: bars (x, height) centred on the canvas. The full mark has five, the favicon three thicker ones.
FULL = {"width": 44, "gap": 28, "heights": [120, 220, 300, 180, 100], "alpha": [0.65, 0.85, 1, 0.85, 0.65]}
SMALL = {"width": 92, "gap": 38, "heights": [190, 330, 240], "alpha": [1, 1, 1]}


def bars(spec):
    n, w, g = len(spec["heights"]), spec["width"], spec["gap"]
    x = (SIZE - (n * w + (n - 1) * g)) / 2
    for h, a in zip(spec["heights"], spec["alpha"]):
        yield x, (SIZE - h) / 2, w, h, a
        x += w + g


def svg(spec, radius=112):
    rects = "\n".join(
        f'  <rect x="{x:g}" y="{y:g}" width="{w}" height="{h}" rx="{w / 2:g}" fill="#fff"'
        + (f' fill-opacity="{a}"' if a < 1 else "") + "/>" for x, y, w, h, a in bars(spec))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" role="img" aria-label="PodcasterNG">\n'
            f'  <rect width="{SIZE}" height="{SIZE}" rx="{radius}" fill="{BLUE}"/>\n{rects}\n</svg>\n')


def png(spec, size, radius=112, scale=1.0, supersample=4):
    """Render the mark. ``scale`` < 1 shrinks the bars (safe zone for round crops); radius 0 gives a full-bleed square."""
    big = size * supersample
    k = big / SIZE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([0, 0, big - 1, big - 1], radius * k, fill=BLUE)
    for x, y, w, h, a in bars(spec):
        layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        cx, cy = SIZE / 2, SIZE / 2
        box = [(cx + (x - cx) * scale) * k, (cy + (y - cy) * scale) * k,
               (cx + (x + w - cx) * scale) * k, (cy + (y + h - cy) * scale) * k]
        ImageDraw.Draw(layer).rounded_rectangle(box, w * scale / 2 * k, fill=(255, 255, 255, round(255 * a)))
        img = Image.alpha_composite(img, layer)
    return img.resize((size, size), Image.LANCZOS)


def main():
    (STATIC / "logo.svg").write_text(svg(FULL))
    (STATIC / "favicon.svg").write_text(svg(SMALL, radius=96))
    png(FULL, 512).save(STATIC / "icon-512.png")
    png(FULL, 192).save(STATIC / "icon-192.png")
    png(FULL, 180, radius=0).save(STATIC / "apple-touch-icon.png")     # iOS rounds the corners itself
    ico = [png(SMALL, s, radius=96) for s in (16, 32, 48)]
    ico[2].save(STATIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)], append_images=ico[:2])
    png(FULL, 640, radius=0, scale=0.9).save(ROOT / "branding" / "telegram-bot.png")


if __name__ == "__main__":
    main()
