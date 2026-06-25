"""Generate NoType icon assets — "Minimal" microphone (variant B).

Produces:
  electron/assets/icon.ico   – multi-size Windows icon (16…256)
  electron/assets/icon.png   – 512px master
  electron/assets/tray-16/32/64.png – transparent tray glyphs

Design: a clean, flat mint microphone (capsule + U cradle + stem + base) on a
deep indigo→midnight rounded tile with a soft mint glow. Everything is drawn at
4× and downsampled with LANCZOS for crisp, near-vector edges, with real alpha
(transparent tile corners, transparent tray background).

Re-run after changing the design — this is a build helper, not runtime code.
"""

import os
import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "electron", "assets")
os.makedirs(ASSETS, exist_ok=True)

# Palette
MINT = (21, 216, 173)          # #15d8ad – the flat mic color
MINT_TRAY = (26, 215, 173)     # slightly punchier for tiny tray sizes
TILE_TOP = (38, 38, 71)        # #262647
TILE_BOT = (19, 19, 29)        # #13131d
GLOW = (0, 212, 170)

SS = 4  # supersample factor


def _vertical_gradient(size, top, bot):
    t = np.linspace(0.0, 1.0, size)[:, None]
    rgb = (np.array(top)[None, :] * (1 - t) + np.array(bot)[None, :] * t)
    arr = np.repeat(rgb[:, None, :], size, axis=1).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def _radial_glow(size, color, cx, cy, r, peak=0.5, falloff=1.7):
    yy, xx = np.mgrid[0:size, 0:size]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / r
    a = np.clip(1.0 - dist, 0.0, 1.0) ** falloff
    rgba = np.zeros((size, size, 4), np.uint8)
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = color
    rgba[..., 3] = (a * 255 * peak).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def _rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def _draw_mic(draw, S, color):
    """Draw the flat 'minimal' microphone glyph on an S×S canvas."""
    cx = 0.5 * S
    lw = 0.046 * S
    # Capsule (rounded pill)
    cw, ch = 0.257 * S, 0.40 * S
    x0, y0 = cx - cw / 2, 0.186 * S
    draw.rounded_rectangle([x0, y0, x0 + cw, y0 + ch], radius=cw / 2, fill=color)
    # U-shaped cradle: bottom half of an ellipse
    ax0, ax1 = 0.286 * S, 0.714 * S
    ay0, ay1 = 0.193 * S, 0.636 * S
    draw.arc([ax0, ay0, ax1, ay1], start=0, end=180, fill=color, width=int(round(lw)))
    # Round caps for the cradle tips
    tip_y = (ay0 + ay1) / 2
    for tx in (ax0, ax1):
        draw.ellipse([tx - lw / 2, tip_y - lw / 2, tx + lw / 2, tip_y + lw / 2], fill=color)
    # Stem
    draw.rounded_rectangle([cx - lw / 2, 0.643 * S, cx + lw / 2, 0.814 * S], radius=lw / 2, fill=color)
    # Base bar
    bw = 0.186 * S
    by = 0.829 * S
    draw.rounded_rectangle([cx - bw / 2, by - lw / 2, cx + bw / 2, by + lw / 2], radius=lw / 2, fill=color)


def _build_app_master():
    """Full app icon (with tile + glow) at high resolution, RGBA with rounded alpha."""
    size = 512 * SS
    tile = _vertical_gradient(size, TILE_TOP, TILE_BOT).convert("RGBA")
    glow = _radial_glow(size, GLOW, cx=size * 0.5, cy=size * 0.42, r=size * 0.5, peak=0.55)
    tile = Image.alpha_composite(tile, glow)
    _draw_mic(ImageDraw.Draw(tile), size, MINT)
    tile.putalpha(_rounded_mask(size, radius=int(size * 0.225)))
    return tile


def _build_tray_master():
    """Transparent tray glyph at high resolution."""
    size = 64 * SS
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _draw_mic(ImageDraw.Draw(img), size, MINT_TRAY)
    return img


def main():
    app = _build_app_master()
    tray = _build_tray_master()

    # Master PNG
    app.resize((512, 512), Image.LANCZOS).save(os.path.join(ASSETS, "icon.png"))

    # Windows .ico (downsample the master per size for crisp small icons)
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    ico_imgs = [app.resize((s, s), Image.LANCZOS) for s in ico_sizes]
    ico_imgs[-1].save(
        os.path.join(ASSETS, "icon.ico"),
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=ico_imgs[:-1],
    )

    # Tray PNGs
    for s in (16, 32, 64):
        tray.resize((s, s), Image.LANCZOS).save(os.path.join(ASSETS, f"tray-{s}.png"))

    print("Wrote:")
    for f in sorted(os.listdir(ASSETS)):
        p = os.path.join(ASSETS, f)
        print(f"  {f}  ({os.path.getsize(p)} bytes)")


if __name__ == "__main__":
    main()
