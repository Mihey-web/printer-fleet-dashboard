// The theme this browser chose in 👤 «Этот браузер» (forge-ops-theme, the same
// rule as app.js initTheme), set on <html> as soon as this file runs: the app
// and the login page both load it, so the login page shows that theme too.
// Nothing else here reaches the page: the file is public (the login page has
// no session), it only holds colour getters.
(function forgeThemeBoot() {
  var root = document.documentElement, t = null;
  try { t = localStorage.getItem("forge-ops-theme"); } catch (e) {}
  if (t === null) t = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "" : "light";
  if (t) root.setAttribute("data-theme", t);
  else root.removeAttribute("data-theme");
  // the browser's chrome follows the page background, read from the CSS token
  var meta = document.querySelector('meta[name="theme-color"]');
  var bg = getComputedStyle(root).getPropertyValue("--bg").trim();
  if (meta && bg) meta.setAttribute("content", bg);
})();
function getStateColors() {
        var style = getComputedStyle(document.documentElement);
        return {
          printing: style.getPropertyValue('--info').trim(),
          finished: style.getPropertyValue('--ok').trim(),
          paused: style.getPropertyValue('--warn').trim(),
          error: style.getPropertyValue('--danger').trim(),
          cancelled: style.getPropertyValue('--gray-9').trim(),
          idle: style.getPropertyValue('--gray-11').trim(),
          offline: style.getPropertyValue('--gray-7').trim(),
          maintenance: style.getPropertyValue('--warn').trim(),
          unknown: style.getPropertyValue('--gray-11').trim()
        };
      }
function getTimelineColors() {
        var style = getComputedStyle(document.documentElement);
        return {
          bgTop: style.getPropertyValue('--tl-bg-top').trim(),
          bgBottom: style.getPropertyValue('--tl-bg-bottom').trim(),
          zebra: style.getPropertyValue('--tl-zebra').trim(),
          gridMajor: style.getPropertyValue('--tl-grid-major').trim(),
          gridMinor: style.getPropertyValue('--tl-grid-minor').trim(),
          axisLine: style.getPropertyValue('--tl-axis-line').trim(),
          axisText: style.getPropertyValue('--tl-axis-text').trim(),
          tickMajor: style.getPropertyValue('--tl-tick-major').trim(),
          tickMinor: style.getPropertyValue('--tl-tick-minor').trim(),
          labelText: style.getPropertyValue('--tl-label-text').trim(),
          labelDot: style.getPropertyValue('--tl-label-dot').trim(),
          barShadow: style.getPropertyValue('--tl-bar-shadow').trim(),
          barHighlight: style.getPropertyValue('--tl-bar-highlight').trim(),
          nowLine: style.getPropertyValue('--tl-now-line').trim(),
          nowText: style.getPropertyValue('--tl-now-text').trim()
        };
      }