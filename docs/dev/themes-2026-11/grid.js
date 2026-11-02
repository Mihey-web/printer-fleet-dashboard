// A contact sheet of screenshots: one row per theme, «до» and «после» side by side.
//   node grid.js <shots dir> <out.png> <screen> <width> [themes] [--single]
// <screen>: printers | ams | … (several: comma-separated); <width>: 1280 | 375; out: .png or .jpg.
// --single: the "after" shots only
// (no «до» column), for a quick look. Images are scaled down to fit; the page is
// rendered by Chromium, so no image library is needed.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const [DIR, OUT, SCREEN, WIDTH] = process.argv.slice(2, 6);
const THEMES = (process.argv[6] && !process.argv[6].startsWith("--") ? process.argv[6]
  : "ocean,dark-classic,light,night,cosmos,amber,glass").split(",");
const SINGLE = process.argv.includes("--single");
const NAMES = { ocean: "Океан", "dark-classic": "Классика", light: "Светлая", night: "Ночь", amber: "Янтарь",
                cosmos: "Космос", glass: "Стекло" };

function uri(file) {
  return fs.existsSync(file) ? "data:image/png;base64," + fs.readFileSync(file).toString("base64") : null;
}

(async () => {
  const phone = Number(WIDTH) < 600;
  const cellW = phone ? 260 : 560;
  const screens = SCREEN.split(",");
  let rows = "";
  for (const th of THEMES) {
    let cells = "";
    for (const sc of screens) {
      for (const tag of SINGLE ? ["after"] : ["before", "after"]) {
        const src = uri(path.join(DIR, `${th}-${tag}-${WIDTH}-${sc}.png`));
        const label = SINGLE ? sc : (tag === "before" ? "до" : "после") + (screens.length > 1 ? " · " + sc : "");
        cells += `<figure><figcaption>${label}</figcaption>${src ? `<img src="${src}">` : `<div class="none">нет темы</div>`}</figure>`;
      }
    }
    rows += `<section><h2>${NAMES[th] || th}</h2><div class="row">${cells}</div></section>`;
  }
  const html = `<!doctype html><meta charset="utf-8"><style>
    body { margin: 0; padding: 24px; background: #15171c; color: #e8eaf0; font: 15px/1.3 "IBM Plex Sans", system-ui, sans-serif; }
    h1 { font-size: 20px; margin: 0 0 16px; font-weight: 600; }
    section { margin-bottom: 22px; }
    h2 { font-size: 16px; margin: 0 0 8px; font-weight: 600; }
    .row { display: flex; gap: 14px; }
    body { width: max-content; }
    figure { margin: 0; width: ${cellW}px; flex: none; }
    figcaption { font-size: 12px; color: #9aa0ad; margin-bottom: 4px; text-transform: uppercase; letter-spacing: .06em; }
    img { width: 100%; display: block; border-radius: 8px; border: 1px solid #2a2e37; }
    .none { height: ${phone ? 560 : 390}px; border: 1px dashed #3a3f4a; border-radius: 8px; display: flex; align-items: center;
            justify-content: center; color: #6b7280; }
  </style><h1>Темы · ${SCREEN} · ${WIDTH} px</h1>${rows}`;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 400, height: 400 } });
  await page.setContent(html);
  await page.waitForTimeout(300);
  const w = await page.evaluate(() => document.documentElement.scrollWidth);
  await page.setViewportSize({ width: w, height: 400 });
  await page.screenshot(Object.assign({ path: OUT, fullPage: true }, /\.jpe?g$/.test(OUT) ? { quality: 88 } : {}));
  await browser.close();
})();
