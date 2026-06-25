"""Generate NoType icon assets from the user-provided mint mic design.

Faithfully reproduces NoType-icon-mint-{squircle,circle}.svg (dark microphone
on a mint radial gradient, soft drop shadow) in Pillow + numpy so we get clean
alpha (transparent outside the rounded tile / circle) and crisp edges via 4×
supersampling — things svglib's renderer doesn't do reliably (filters +
transparency).

  App icon  → squircle  (Windows 11 app-tile convention)  → icon.ico / icon.png
  Tray icon → circle    (compact badge reads best at 16px) → tray-16/32/64.png

All glyph/shape coordinates are in the SVG's 0–100 user space.
Re-run after changing the design — build helper, not runtime code.
"""

import os
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageChops

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "electron", "assets")
os.makedirs(ASSETS, exist_ok=True)

MASTER = 2048                    # supersampled master resolution
GLYPH = (8, 20, 15, 255)         # #08140F dark microphone
SHADOW = (5, 58, 44)             # #053a2c drop-shadow flood
# Radial gradient stops (offset, RGB): #33E6C0 → #18C9A2 → #15C39C
GRAD = [(0.0, (51, 230, 192)), (0.70, (24, 201, 162)), (1.0, (21, 195, 156))]


def _k(v):
    return v * MASTER / 100.0


def _radial_mint(size):
    """Radial gradient, center (50%, 34%), radius 62% — matches the SVG."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    d = np.sqrt(((xx / size) - 0.50) ** 2 + ((yy / size) - 0.34) ** 2) / 0.62
    d = np.clip(d, 0.0, 1.0)
    (o0, c0), (o1, c1), (o2, c2) = GRAD
    c0, c1, c2 = map(np.array, (c0, c1, c2))
    rgb = np.empty((size, size, 3), np.float64)
    lo = d <= o1
    t = (d[lo] - o0) / (o1 - o0)
    rgb[lo] = c0 + (c1 - c0) * t[:, None]
    hi = ~lo
    t = (d[hi] - o1) / (o2 - o1)
    rgb[hi] = c1 + (c2 - c1) * t[:, None]
    return Image.fromarray(rgb.astype(np.uint8), "RGB")


def _draw_glyph(draw, color):
    """Dark microphone: capsule + U cradle + stem + base (SVG 0–100 space)."""
    lw = _k(6.5)
    # Capsule
    draw.rounded_rectangle([_k(38), _k(18), _k(62), _k(56)], radius=_k(12), fill=color)
    # U cradle — bottom semicircle of a circle centred (50,45) r21
    draw.arc([_k(29), _k(24), _k(71), _k(66)], start=0, end=180, fill=color, width=int(round(lw)))
    for cx, cy in ((29, 45), (71, 45)):  # round caps on the cradle tips
        draw.ellipse([_k(cx) - lw / 2, _k(cy) - lw / 2, _k(cx) + lw / 2, _k(cy) + lw / 2], fill=color)
    # Stem
    draw.line([_k(50), _k(66), _k(50), _k(80)], fill=color, width=int(round(lw)))
    draw.ellipse([_k(50) - lw / 2, _k(66) - lw / 2, _k(50) + lw / 2, _k(66) + lw / 2], fill=color)
    # Base bar
    draw.rounded_rectangle([_k(39), _k(80), _k(61), _k(86)], radius=_k(3), fill=color)


def _shape_mask(size, shape):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    if shape == "circle":
        d.ellipse([_k(3), _k(3), _k(97), _k(97)], fill=255)
    else:  # squircle
        d.rounded_rectangle([_k(3), _k(3), _k(97), _k(97)], radius=_k(21), fill=255)
    return m


def _build(shape):
    size = MASTER
    # Mint tile clipped to the shape
    tile = _radial_mint(size).convert("RGBA")
    tile.putalpha(_shape_mask(size, shape))

    # Soft drop shadow under the glyph (dy≈9, blur≈8, 22% opacity)
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _draw_glyph(ImageDraw.Draw(shadow), (*SHADOW, 56))
    shadow = ImageChops.offset(shadow, 0, int(_k(9)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(_k(8)))
    shadow.putalpha(ImageChops.multiply(shadow.getchannel("A"), _shape_mask(size, shape)))
    tile = Image.alpha_composite(tile, shadow)

    # Glyph on top
    glyph = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _draw_glyph(ImageDraw.Draw(glyph), GLYPH)
    tile = Image.alpha_composite(tile, glyph)
    return tile


def main():
    app = _build("squircle")
    tray = _build("circle")

    app.resize((512, 512), Image.LANCZOS).save(os.path.join(ASSETS, "icon.png"))

    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    ico_imgs = [app.resize((s, s), Image.LANCZOS) for s in ico_sizes]
    ico_imgs[-1].save(
        os.path.join(ASSETS, "icon.ico"),
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=ico_imgs[:-1],
    )

    for s in (16, 32, 64):
        tray.resize((s, s), Image.LANCZOS).save(os.path.join(ASSETS, f"tray-{s}.png"))

    print("Wrote:")
    for f in sorted(os.listdir(ASSETS)):
        p = os.path.join(ASSETS, f)
        print(f"  {f}  ({os.path.getsize(p)} bytes)")


if __name__ == "__main__":
    main()
