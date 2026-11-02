// Screenshots of the panel in every theme, on the synthetic farm (demo_farm.py).
//   node shoot.js <base url> <out dir> <tag> [theme,theme,...] [--glass]
// <tag> goes into the file names: before / after. Themes by their data-theme value,
// "ocean" for the default (no attribute). Per theme: the printers and the AMS screen
// at 1280 and 375 px; with --glass also the history and admin screens, the 👤 menu
// and the login page.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const [BASE, OUT, TAG] = process.argv.slice(2, 5);
const THEMES = (process.argv[5] && !process.argv[5].startsWith("--") ? process.argv[5] : "ocean,dark-classic,light,night,cosmos,amber").split(",");
const GLASS_EXTRA = process.argv.includes("--glass");
const SIZES = [{ w: 1280, h: 900, scale: 1, mobile: false }, { w: 375, h: 812, scale: 2, mobile: true }];

async function settle(page, ms) {
  await page.waitForTimeout(ms || 1800);
  // no caret, no hover leftovers
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

async function shot(page, name) {
  const file = path.join(OUT, name + ".png");
  await page.screenshot({ path: file, fullPage: false });
  return file;
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  for (const sz of SIZES) {
    for (const theme of THEMES) {
      const value = theme === "ocean" ? "" : theme;
      const ctx = await browser.newContext({ viewport: { width: sz.w, height: sz.h }, deviceScaleFactor: sz.scale,
                                             isMobile: sz.mobile, hasTouch: sz.mobile, locale: "ru-RU",
                                             colorScheme: "dark" });
      await ctx.addInitScript((v) => {
        try { localStorage.setItem("forge-ops-theme", v); localStorage.setItem("forge-ops-lang", "ru"); } catch (e) {}
      }, value);
      const page = await ctx.newPage();
      page.on("pageerror", (e) => console.error(theme, sz.w, "pageerror", e.message));
      const pre = `${theme}-${TAG}-${sz.w}`;
      if (GLASS_EXTRA) {
        await page.goto(BASE + "/");
        await page.waitForSelector("#u");
        await settle(page, 800);
        await shot(page, `${pre}-login`);
      }
      await login(page);
      await settle(page, 2600);
      await shot(page, `${pre}-printers`);
      await page.goto(BASE + "/#/ams");
      await settle(page, 2200);
      await shot(page, `${pre}-ams`);
      if (GLASS_EXTRA) {
        await page.goto(BASE + "/#/history/chrono?period=24h");
        await settle(page, 2500);
        await shot(page, `${pre}-history`);
        await page.goto(BASE + "/#/admin/park");
        await settle(page, 2000);
        await shot(page, `${pre}-admin`);
        await page.goto(BASE + "/#/");
        await settle(page, 1500);
        await page.click("#meBtn");
        await settle(page, 700);
        await shot(page, `${pre}-me`);
        await page.keyboard.press("Escape");
        await settle(page, 400);
        // the bell's popover: a blurred surface over the cards
        if (await page.$("#nfBellSlot button")) {
          await page.click("#nfBellSlot button");
          await settle(page, 700);
          await shot(page, `${pre}-bell`);
          await page.keyboard.press("Escape");
        }
        // the fallback: the system asks for less transparency (the same dense fills as without backdrop-filter)
        const cdp = await ctx.newCDPSession(page);
        await cdp.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-transparency", value: "reduce" }] });
        await page.goto(BASE + "/#/");
        await settle(page, 2000);
        await shot(page, `${pre}-reduced`);
      }
      await ctx.close();
      console.log("done", pre);
    }
  }
  await browser.close();
})();
