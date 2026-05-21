"""Generate NoType icon assets.

Produces:
  electron/assets/icon.ico  – multi-size Windows icon (16/24/32/48/64/128/256)
  electron/assets/icon.png  – 512px source PNG
  electron/assets/tray-16.png, tray-32.png, tray-64.png – tray icons

Design: a stylised microphone in NoType's mint accent (#00d4aa) on a dark
rounded background, rendered at 4× and downsampled with LANCZOS for clean edges.

Re-run this script whenever you change the design – it is not part of the
runtime, just a one-shot build helper.
"""

import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "electron", "assets")
os.makedirs(ASSETS, exist_ok=True)

# Palette
BG = (26, 26, 46, 255)        # #1a1a2e – deep midnight
ACCENT = (0, 212, 170, 255)   # #00d4aa – mint
ACCENT_DIM = (0, 212, 170, 140)


def render(size: int, *, transparent_bg: bool = False) -> Image.Image:
    """Render the NoType icon at the requested size (square).

    Layout (vertical, as fraction of canvas):
      0.18 .. 0.50   capsule (rounded pill)
      0.42 .. 0.70   U-shaped cradle (lower half of an arc bbox), wider than capsule
      0.70 .. 0.82   vertical stem
      0.82           horizontal base bar
    """
    s = size * 4  # 4x supersample for clean LANCZOS down-scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    line_w = max(2, int(s * 0.045))  # mic line thickness

    if not transparent_bg:
        margin = s // 24
        d.rounded_rectangle(
            [margin, margin, s - margin, s - margin],
            radius=int(s * 0.22),
            fill=BG,
        )

    # ── Capsule ──
    cap_w = int(s * 0.30)
    cap_h = int(s * 0.34)            # 0.18 → 0.52
    cap_x = (s - cap_w) // 2
    cap_y = int(s * 0.18)
    d.rounded_rectangle(
        [cap_x, cap_y, cap_x + cap_w, cap_y + cap_h],
        radius=cap_w // 2,
        fill=ACCENT,
    )

    # ── U-shaped cradle ──
    # Arc bounding box spans more vertical room than is drawn, because only the
    # lower half of a full circle is rendered (PIL arc start=0 end=180).
    u_w = int(s * 0.52)              # wider than capsule – embraces it
    u_x = (s - u_w) // 2
    # We want the visible arc to occupy roughly 0.46 → 0.70 vertical.
    # arc bbox height = u_w (square box → semicircle). Visual bottom is at bbox_y + u_w/2.
    visual_bottom = int(s * 0.70)
    bbox_y = visual_bottom - u_w // 2
    d.arc(
        [u_x, bbox_y, u_x + u_w, bbox_y + u_w],
        start=0, end=180,            # lower semicircle (opens upward)
        fill=ACCENT,
        width=line_w,
    )

    # ── Stem ──
    stem_y1 = visual_bottom - line_w // 2
    stem_y2 = int(s * 0.83)
    d.line([s // 2, stem_y1, s // 2, stem_y2], fill=ACCENT, width=line_w)

    # ── Base ──
    base_w = int(s * 0.22)
    base_x = (s - base_w) // 2
    d.line([base_x, stem_y2, base_x + base_w, stem_y2], fill=ACCENT, width=line_w)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    # Master PNG
    master = render(512)
    master.save(os.path.join(ASSETS, "icon.png"))

    # Windows .ico bundle (Windows picks the closest size at runtime)
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    ico_images = [render(s) for s in ico_sizes]
    # PIL's ICO writer takes one base image + a `sizes` list and embeds all.
    ico_images[-1].save(
        os.path.join(ASSETS, "icon.ico"),
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=ico_images[:-1],
    )

    # Tray icons (transparent background, the mic glyph only)
    for s in (16, 32, 64):
        render(s, transparent_bg=True).save(os.path.join(ASSETS, f"tray-{s}.png"))

    print("Wrote:")
    for f in sorted(os.listdir(ASSETS)):
        path = os.path.join(ASSETS, f)
        print(f"  {f}  ({os.path.getsize(path)} bytes)")


if __name__ == "__main__":
    main()
