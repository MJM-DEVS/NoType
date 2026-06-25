// Faithfully rasterize an SVG to a transparent PNG via Chromium (Electron).
// Usage: electron.exe svg_to_png.js <svgPath> <outPath> <size>
// Renders the SVG exactly as authored (gradients, filters, round caps, alpha) —
// no re-interpretation. The window is positioned off-screen so it never shows.
const { app, BrowserWindow } = require('electron');
const fs = require('fs');

const svgPath = process.argv[2];
const outPath = process.argv[3];
const size = parseInt(process.argv[4], 10) || 1024;

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    x: -4000, y: -4000, width: size, height: size,
    show: true, frame: false, transparent: true, skipTaskbar: true,
    resizable: false, backgroundColor: '#00000000',
    webPreferences: { backgroundThrottling: false },
  });

  const svg = fs.readFileSync(svgPath, 'utf8');
  const html =
    '<!doctype html><meta charset="utf-8">' +
    '<style>*{margin:0;padding:0}html,body{background:transparent}' +
    `svg{display:block;width:${size}px;height:${size}px}</style>` + svg;

  await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
  await new Promise((r) => setTimeout(r, 500));  // let the gradient/filter paint
  const img = await win.webContents.capturePage();
  fs.writeFileSync(outPath, img.toPNG());
  app.quit();
}).catch((e) => { console.error(e); app.exit(1); });
