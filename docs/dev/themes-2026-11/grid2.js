// Contact sheets for the second round (shots from shoot2.js).
//   node grid2.js blind <shots dir> <out.jpg> <width>
//     The blind test: the «Принтеры» screen of every theme, no captions on the pictures; the
//     names are a separate line under the grid, in the grid's order (left to right, top down).
//   node grid2.js glass <shots dir> <out.jpg>
//     «Стекло»: every screen at 1280 and 375 px, in the dark and the light scheme.
// Rendered by Chromium (no image library needed); pictures scaled down to fit.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const [MODE, DIR, OUT, WIDTH] = process.argv.slice(2, 6);
const ORDER = ["ocean", "dark-classic", "light", "night", "cosmos", "amber", "glass"];
const NAMES = { ocean: "Океан", "dark-classic": "Классика", light: "Светлая", night: "Ночь", cosmos: "Космос",
                amber: "Янтарь", glass: "Стекло" };
const SCREENS = { printers: "Принтеры", ams: "AMS", history: "История", admin: "Админ", me: "👤", bell: "Колокольчик", login: "Вход" };

function uri(file) {
  if (!fs.existsSync(file)) throw new Error("no shot: " + file);
  return "data:image/png;base64," + fs.readFileSync(file).toString("base64");
}

function blind() {
  const phone = Number(WIDTH) < 600;
  const cols = phone ? 7 : 4;
  const cellW = phone ? 230 : 600;
  const imgs = ORDER.map((th) => `<img src="${uri(path.join(DIR, `${th}-v2-dark-${WIDTH}-printers.png`))}">`).join("");
  const names = ORDER.map((th) => NAMES[th]).join(" · ");
  return `<style>.g { display: grid; grid-template-columns: repeat(${cols}, ${cellW}px); gap: 16px; }
    .g img { width: 100%; display: block; border-radius: 10px; }
    .names { margin-top: 18px; font-size: 18px; color: #c8ccd6; }</style>
    <div class="g">${imgs}</div>
    <div class="names">Слева направо${phone ? "" : ", сверху вниз"}: ${names}</div>`;
}

function glass() {
  let html = "<style>.row { display: flex; gap: 12px; margin-bottom: 14px; align-items: flex-start; }"
    + " figure { margin: 0; } figcaption { font-size: 13px; color: #9aa0ad; margin-bottom: 4px; }"
    + " .w1280 img { width: 400px; } .w375 img { width: 170px; } img { display: block; border-radius: 8px; }"
    + " h2 { font-size: 17px; margin: 18px 0 8px; font-weight: 600; }</style>";
  for (const scheme of ["light", "dark"]) {
    for (const w of [1280, 375]) {
      html += `<h2>«Стекло» · ${scheme === "light" ? "светлый" : "тёмный"} режим · ${w} px</h2><div class="row w${w}">`;
      for (const sc of Object.keys(SCREENS)) {
        const f = path.join(DIR, `glass-v2-${scheme}-${w}-${sc}.png`);
        if (!fs.existsSync(f)) continue;
        html += `<figure><figcaption>${SCREENS[sc]}</figcaption><img src="${uri(f)}"></figure>`;
      }
      html += "</div>";
    }
  }
  return html;
}

(async () => {
  const body = MODE === "blind" ? blind() : glass();
  const html = `<!doctype html><meta charset="utf-8"><style>
    body { margin: 0; padding: 24px; background: #15171c; color: #e8eaf0; font: 15px/1.3 system-ui, sans-serif; width: max-content; }
  </style>${body}`;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 400, height: 400 } });
  await page.setContent(html);
  await page.waitForTimeout(300);
  const w = await page.evaluate(() => document.documentElement.scrollWidth);
  await page.setViewportSize({ width: w, height: 400 });
  await page.screenshot({ path: OUT, fullPage: true, type: "jpeg", quality: 86 });
  await browser.close();
})();
