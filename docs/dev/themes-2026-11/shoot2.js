// Screenshots for the second round (v2), on the synthetic farm (demo_farm.py).
//   node shoot2.js <base url> <out dir> [--themes a,b] [--screens printers,ams,...] [--widths 1280,375]
//                  [--scheme dark|light] [--tag v2]
// Files: <theme>-<tag>-<scheme>-<width>-<screen>.png. Themes by their data-theme value, "ocean"
// for the default (no attribute). Screens: printers, ams, history, admin, me (the 👤 menu),
// bell (the bell's popover), login. --scheme is the system's prefers-color-scheme: «Стекло»
// follows it, the other themes do not care.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const [BASE, OUT] = process.argv.slice(2, 4);
function opt(name, def) {
  const i = process.argv.indexOf("--" + name);
  return i > 0 ? process.argv[i + 1] : def;
}
const THEMES = opt("themes", "ocean,dark-classic,light,night,cosmos,amber,glass").split(",");
const SCREENS = opt("screens", "printers").split(",");
const WIDTHS = opt("widths", "1280,375").split(",").map(Number);
const SCHEME = opt("scheme", "dark");
const TAG = opt("tag", "v2");

async function settle(page, ms) {
  await page.waitForTimeout(ms || 1800);
  await page.mouse.move(0, 0);
}

async function login(page) {
  await page.goto(BASE + "/");
  if (await page.$("#u")) {
    await page.fill("#u", "admin");
    await page.fill("#p", "demo-pass-123");
    await page.click("#b");
  }
  await page.waitForSelector(".printer-card", { timeout: 15000 });
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  for (const w of WIDTHS) {
    const mobile = w < 600;
    for (const theme of THEMES) {
      const value = theme === "ocean" ? "" : theme;
      const ctx = await browser.newContext({ viewport: { width: w, height: mobile ? 812 : 900 }, deviceScaleFactor: mobile ? 2 : 1,
                                             isMobile: mobile, hasTouch: mobile, locale: "ru-RU", colorScheme: SCHEME });
      await ctx.addInitScript((v) => {
        try { localStorage.setItem("forge-ops-theme", v); localStorage.setItem("forge-ops-lang", "ru"); } catch (e) {}
      }, value);
      const page = await ctx.newPage();
      page.on("pageerror", (e) => console.error(theme, w, "pageerror", e.message));
      const pre = path.join(OUT, `${theme}-${TAG}-${SCHEME}-${w}`);
      const shot = (sc) => page.screenshot({ path: `${pre}-${sc}.png` });
      if (SCREENS.includes("login")) {
        await page.goto(BASE + "/");
        await page.waitForSelector("#u");
        await settle(page, 900);
        await shot("login");
      }
      await login(page);
      await settle(page, 2600);
      for (const sc of SCREENS) {
        if (sc === "login") continue;
        if (sc === "printers") { await page.goto(BASE + "/#/"); await settle(page, 1500); }
        else if (sc === "ams") { await page.goto(BASE + "/#/ams"); await settle(page, 2200); }
        else if (sc === "history") { await page.goto(BASE + "/#/history/chrono?period=24h"); await settle(page, 2500); }
        else if (sc === "admin") { await page.goto(BASE + "/#/admin/park"); await settle(page, 2000); }
        else if (sc === "me") {
          await page.goto(BASE + "/#/"); await settle(page, 1200);
          await page.click("#meBtn"); await settle(page, 700);
        } else if (sc === "bell") {
          await page.goto(BASE + "/#/"); await settle(page, 1200);
          if (!(await page.$("#nfBellSlot button"))) continue;
          await page.click("#nfBellSlot button"); await settle(page, 700);
        }
        await shot(sc);
        if (sc === "me" || sc === "bell") { await page.keyboard.press("Escape"); await settle(page, 300); }
      }
      await ctx.close();
      console.log("done", pre);
    }
  }
  await browser.close();
})();
