// Live EN check of every screen at 1280 and 375 px: screenshots, visible Cyrillic, console errors.
// node check_en.js <base url> <out dir>
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const BASE = process.argv[2] || "http://127.0.0.1:8765";
const OUT = process.argv[3] || "shots";
// the user's own: printer and user names may stay Cyrillic
// the language's own name in the switcher; a print job's file name (the slicer's, user data)
const ALLOWED = [/Принтер у окна/g, /Русский/g, /Корпус_пульта_v2/g, /Замена сопла и чистка экструдера/g];

const problems = [];
const consoleErrors = [];
const missingAll = new Set();
const perf = [];

function cyrLines(text) {
  let t = text;
  ALLOWED.forEach((re) => { t = t.replace(re, ""); });
  return t.split("\n").map((s) => s.trim()).filter((s) => /[А-Яа-яЁё]/.test(s));
}

async function visible(page) {
  return page.evaluate(() => {
    const out = [];
    const vis = (el) => {
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height) return false;
      const cs = getComputedStyle(el);
      return cs.visibility !== "hidden" && cs.display !== "none";
    };
    out.push(document.body.innerText);
    document.querySelectorAll("[data-tip],[aria-label],[placeholder],[title],[alt],[data-l]").forEach((el) => {
      if (!vis(el)) return;
      ["data-tip", "aria-label", "placeholder", "title", "alt", "data-l"].forEach((a) => {
        const v = el.getAttribute(a);
        if (v) out.push(v);
      });
    });
    // ::before texts of the CSS (pauses, sign-in)
    document.querySelectorAll("td").forEach((el) => {
      const c = getComputedStyle(el, "::before").content;
      if (c && c !== "none" && c !== "normal" && vis(el)) out.push(c.replace(/^"|"$/g, ""));
    });
    out.push(Object.getOwnPropertyDescriptor(Document.prototype, "title").get.call(document));
    return out.join("\n");
  });
}

async function check(page, name, width) {
  await page.waitForTimeout(400);
  const file = path.join(OUT, `${width}-${name}.png`);
  await page.screenshot({ path: file, fullPage: width < 600 ? false : true });
  const lines = cyrLines(await visible(page));
  const miss = await page.evaluate(() => (window.I18N ? I18N.missing() : []));
  miss.forEach((m) => missingAll.add(m));
  if (lines.length) problems.push({ screen: `${width}-${name}`, lines: [...new Set(lines)].slice(0, 40) });
  // horizontal overflow of the page on a phone
  if (width < 600) {
    const ov = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    if (ov > 1) problems.push({ screen: `${width}-${name}`, overflow: ov });
  }
}

async function tryClick(page, sel, opts) {
  const el = await page.$(sel);
  if (!el) return false;
  try { await el.click(Object.assign({ timeout: 2000 }, opts || {})); return true; } catch (e) { return false; }
}

async function closeAll(page) {
  await page.keyboard.press("Escape");
  await page.waitForTimeout(150);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(150);
}

async function run(width, height, mobile) {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1, isMobile: mobile, hasTouch: mobile,
                                         locale: "en-US" });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(`${width}: ${m.text()}`); });
  page.on("pageerror", (e) => consoleErrors.push(`${width}: pageerror ${e.message}`));
  page.on("response", (r) => { if (r.status() >= 400) consoleErrors.push(`${width}: HTTP ${r.status()} ${r.url()}`); });

  // the login page: English from navigator.languages (en-US)
  await page.goto(BASE + "/");
  await check(page, "login", width);
  await page.fill("#u", "admin");
  await page.fill("#p", "demo-pass-123");
  await page.click("#b");
  await page.waitForSelector(".printer-card", { timeout: 15000 });
  await page.waitForTimeout(2500);
  await check(page, "printers", width);

  // popovers and sheets of the printers screen
  const openers = [
    ["bell", "#nfBellSlot button"],
    ["card-notice", ".printer-card [aria-haspopup='dialog']"],
    ["card-menu", ".printer-card .pc-ctl-head"],
    ["temps", ".printer-card .pc-chips > [tabindex], .printer-card .pc-chips > [role=button], .printer-card .pc-chips > button"],
    ["ams-chip", ".printer-card .pc-ams [tabindex], .printer-card [class*=ams] [role=button]"],
    ["finishes", "#finishRow button"],
    ["farm-sheet", "#farmSheetBtn"],
  ];
  for (const [nm, sel] of openers) {
    if (await tryClick(page, sel)) { await check(page, "printers-" + nm, width); await closeAll(page); }
  }
  // the cost of translating while the cards refresh (every 2 s) for 30 s
  if (width > 600) {
    const a = await page.evaluate(() => I18N.stats());
    await page.waitForTimeout(30000);
    const b = await page.evaluate(() => I18N.stats());
    perf.push({ width, seconds: 30, batches: b.batches - a.batches, ms: +(b.ms - a.ms).toFixed(1), writes: b.writes - a.writes, maxMs: +b.maxMs.toFixed(1) });
  }
  // 👤 panel
  if (await tryClick(page, "#meBtn")) { await check(page, "me", width); await closeAll(page); }

  const routes = [
    ["ams", "#/ams"], ["history-jobs", "#/history/jobs?period=7d"], ["history-chrono", "#/history/chrono?period=7d"],
    ["history-stats", "#/history/stats?period=7d"], ["history-actions", "#/history/actions?period=7d"],
    ["admin-park", "#/admin/park"], ["admin-auto", "#/admin/auto"], ["admin-access", "#/admin/access"],
    ["admin-system", "#/admin/system"],
  ];
  for (const [nm, hash] of routes) {
    await page.goto(BASE + "/" + hash);
    await page.waitForTimeout(2500);
    await check(page, nm, width);
    if (nm === "history-jobs" && await tryClick(page, "#jobsBody tr")) { await check(page, nm + "-row", width); }
    if (nm === "ams" && await tryClick(page, "#amsGrid button")) { await check(page, nm + "-pop", width); await closeAll(page); }
    if (nm === "admin-park" && await tryClick(page, "#addPrinterBtn")) { await check(page, nm + "-form", width); }
    if (nm === "admin-system" && await tryClick(page, "#tgDetails summary")) { await check(page, nm + "-details", width); }
    if (nm === "admin-auto") {
      await page.evaluate(() => document.querySelectorAll("details").forEach((d) => { d.open = true; }));
      await check(page, nm + "-open", width);
    }
  }
  await browser.close();
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  await run(1280, 900, false);
  await run(375, 812, true);
  fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify({ problems, consoleErrors, perf, missing: [...missingAll] }, null, 1));
  console.log(JSON.stringify({ problems: problems.length, consoleErrors: consoleErrors.length, perf }));
})();
