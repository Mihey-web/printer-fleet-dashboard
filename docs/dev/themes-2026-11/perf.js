// The cost of a theme: frame times while scrolling, and an idle tab.
//   node perf.js <base url> <themes, comma-separated; "glass:light" for a scheme> [out.json]
// Per theme and width (375 px phone with the CPU slowed 4×, as a mid phone; 1280 px desktop):
// * scroll: the «Принтеры» page scrolled down and back by 14 px per frame for 4 s;
//   the frame interval from requestAnimationFrame: mean, p95, frames over 25 ms;
// * idle: 10 s with nothing touched (the panel still polls every 2 s, as live):
//   frames drawn (Chrome trace, DrawFrame) and the CPU time of all Chromium processes
//   (headless Chromium renders with SwiftShader, so its GPU work is CPU time here).
const { chromium } = require("playwright");
const fs = require("fs");

const [BASE, THEMES_ARG, OUT] = process.argv.slice(2, 5);
const THEMES = (THEMES_ARG || "ocean,glass").split(",");
const CLK = 100; // USER_HZ

function cpuTicks(rootPid) {
  // every descendant of rootPid (not itself: node only waits meanwhile)
  const kids = {};
  for (const d of fs.readdirSync("/proc")) {
    if (!/^\d+$/.test(d)) continue;
    try {
      const st = fs.readFileSync(`/proc/${d}/stat`, "utf8");
      const f = st.slice(st.lastIndexOf(")") + 2).split(" ");
      const ppid = Number(f[1]);
      (kids[ppid] = kids[ppid] || []).push({ pid: Number(d), ticks: Number(f[11]) + Number(f[12]) });
    } catch (e) {}
  }
  let sum = 0;
  const walk = (pid) => (kids[pid] || []).forEach((k) => { sum += k.ticks; walk(k.pid); });
  walk(rootPid);
  return sum;
}

async function measure(browser, themeArg, sz) {
  // "glass:light" — a theme in the system's light scheme (only «Стекло» follows it); dark by default
  const [theme, scheme] = themeArg.split(":");
  const value = theme === "ocean" ? "" : theme;
  const ctx = await browser.newContext({ viewport: { width: sz.w, height: sz.h }, deviceScaleFactor: sz.scale,
                                         isMobile: sz.mobile, hasTouch: sz.mobile, locale: "ru-RU",
                                         colorScheme: scheme || "dark" });
  await ctx.addInitScript((v) => { try { localStorage.setItem("forge-ops-theme", v); } catch (e) {} }, value);
  const page = await ctx.newPage();
  await page.goto(BASE + "/");
  if (await page.$("#u")) {
    await page.fill("#u", "admin");
    await page.fill("#p", "demo-pass-123");
    await page.click("#b");
  }
  await page.waitForSelector(".printer-card", { timeout: 15000 });
  await page.waitForTimeout(3000);
  const cdp = await ctx.newCDPSession(page);
  if (sz.cpu > 1) await cdp.send("Emulation.setCPUThrottlingRate", { rate: sz.cpu });

  // scroll
  const scroll = await page.evaluate(() => new Promise((done) => {
    const times = [];
    const max = document.documentElement.scrollHeight - innerHeight;
    let dir = 1, t0 = performance.now();
    function step(t) {
      times.push(t);
      let y = scrollY + dir * 14;
      if (y >= max) { y = max; dir = -1; } else if (y <= 0) { y = 0; dir = 1; }
      scrollTo(0, y);
      if (t - t0 < 4000) requestAnimationFrame(step); else done({ times, max });
    }
    requestAnimationFrame(step);
  }));
  const d = [];
  for (let i = 1; i < scroll.times.length; i++) d.push(scroll.times[i] - scroll.times[i - 1]);
  d.sort((a, b) => a - b);
  const mean = d.reduce((a, b) => a + b, 0) / d.length;
  const p95 = d[Math.floor(d.length * 0.95)];
  const long = d.filter((x) => x > 25).length;
  await page.evaluate(() => scrollTo(0, 0));
  if (sz.cpu > 1) await cdp.send("Emulation.setCPUThrottlingRate", { rate: 1 });
  await page.waitForTimeout(1500);

  // idle
  await browser.startTracing(page, { categories: ["disabled-by-default-devtools.timeline.frame", "devtools.timeline"] });
  const c0 = cpuTicks(global.BROWSER_PID);
  await page.waitForTimeout(10000);
  const c1 = cpuTicks(global.BROWSER_PID);
  const trace = JSON.parse((await browser.stopTracing()).toString());
  const ev = trace.traceEvents || trace;
  const frames = ev.filter((e) => e.name === "DrawFrame").length;
  await ctx.close();
  return {
    theme: themeArg, width: sz.w, cpu_throttle: sz.cpu,
    scroll: { frames: d.length + 1, mean_ms: +mean.toFixed(1), p95_ms: +p95.toFixed(1), over_25ms: long, page_px: scroll.max },
    idle_10s: { frames_drawn: frames, cpu_s: +((c1 - c0) / CLK).toFixed(2) },
  };
}

(async () => {
  const browser = await chromium.launch({ args: ["--enable-gpu-rasterization"] });
  // every process this node started (the browser and its renderer, GPU and utility children)
  global.BROWSER_PID = process.pid;
  const sizes = [{ w: 375, h: 812, scale: 2, mobile: true, cpu: 4 }, { w: 1280, h: 900, scale: 1, mobile: false, cpu: 1 }];
  const out = [];
  for (const sz of sizes) {
    for (const th of THEMES) {
      const r = await measure(browser, th, sz);
      out.push(r);
      console.log(JSON.stringify(r));
    }
  }
  await browser.close();
  if (OUT) fs.writeFileSync(OUT, JSON.stringify(out, null, 1));
})();
