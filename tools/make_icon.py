"""Generate NoType icon assets by faithfully rasterizing the source SVGs.

The icon design lives in tools/icon-{squircle,circle}.svg (a dark microphone on
a mint radial gradient with a soft drop shadow). We render those EXACT files via
Chromium (Electron, see svg_to_png.js) so gradients, the drop-shadow filter,
round line-caps and transparency come out 1:1 — then only downsample with
Pillow. Nothing is re-drawn in Python.

  App icon  → squircle  (Windows 11 app-tile convention)  → icon.ico / icon.png
  Tray icon → circle    (compact badge reads best at 16px) → tray-16/32/64.png

Run after changing the SVGs — build helper, not runtime code.
"""

import os
import subprocess
from PIL import Image

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
ASSETS = os.path.join(ROOT, "electron", "assets")
ELECTRON = os.path.join(ROOT, "electron", "node_modules", "electron", "dist", "electron.exe")
RASTERIZER = os.path.join(TOOLS, "svg_to_png.js")

os.makedirs(ASSETS, exist_ok=True)


def _render(svg_name, out_path, size=1024):
    """Rasterize a source SVG to a transparent PNG via Chromium."""
    svg = os.path.join(TOOLS, svg_name)
    subprocess.run([ELECTRON, RASTERIZER, svg, out_path, str(size)], check=True)
    return Image.open(out_path).convert("RGBA")


def main():
    app_master = os.path.join(TOOLS, "_app_master.png")
    tray_master = os.path.join(TOOLS, "_tray_master.png")
    app = _render("icon-squircle.svg", app_master, 1024)
    tray = _render("icon-circle.svg", tray_master, 1024)

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

    for tmp in (app_master, tray_master):
        try:
            os.remove(tmp)
        except OSError:
            pass

    print("Wrote:")
    for f in sorted(os.listdir(ASSETS)):
        p = os.path.join(ASSETS, f)
        print(f"  {f}  ({os.path.getsize(p)} bytes)")


if __name__ == "__main__":
    main()
