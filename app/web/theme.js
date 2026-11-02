// The theme this browser chose in 👤 «Этот браузер» (forge-ops-theme, the same
// rule as app.js initTheme), set on <html> as soon as this file runs: the app
// and the login page both load it, so the login page shows that theme too.
// Nothing else here reaches the page: the file is public (the login page has
// no session), it only holds colour getters and this browser's own photo for
// «Стекло», read from this browser's storage.
(function forgeThemeBoot() {
  var root = document.documentElement, t = null;
  try { t = localStorage.getItem("forge-ops-theme"); } catch (e) {}
  // «Неон» is gone: whoever kept it moves to «Янтарь», silently and once (app.js initTheme, the same)
  if (t === "neon") { t = "amber"; try { localStorage.setItem("forge-ops-theme", t); } catch (e) {} }
  if (t === null) t = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "" : "light";
  if (t) root.setAttribute("data-theme", t);
  else root.removeAttribute("data-theme");
  // the browser's chrome follows the page background, read from the CSS token
  var meta = document.querySelector('meta[name="theme-color"]');
  var bg = getComputedStyle(root).getPropertyValue("--bg").trim();
  if (meta && bg) meta.setAttribute("content", bg);
  applyGlassPhoto();
})();
// The photo this browser put under «Стекло» (👤 → «Своё фото», app.js glassPhotoPick): a JPEG
// data URL in forge-ops-glass-photo, shown through a blob: address, so the page's style holds
// a short link and not the picture. app.js calls it again after a pick or «Убрать фото».
var glassPhotoUrl = null;
function applyGlassPhoto() {
  var root = document.documentElement, data = null, url = null;
  try { data = localStorage.getItem("forge-ops-glass-photo"); } catch (e) {}
  var m = data && /^data:(image\/[a-z0-9.+-]+);base64,/.exec(data);
  if (m) {
    try {
      var bin = atob(data.slice(m[0].length)), bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      url = URL.createObjectURL(new Blob([bytes], { type: m[1] }));
    } catch (e) { url = null; }
  }
  if (url) {
    root.style.setProperty("--glass-photo", 'url("' + url + '")');
    root.setAttribute("data-glass-photo", "");
  } else {
    root.style.removeProperty("--glass-photo");
    root.removeAttribute("data-glass-photo");
  }
  if (glassPhotoUrl) URL.revokeObjectURL(glassPhotoUrl);
  glassPhotoUrl = url;
}
function getStateColors() {
        var style = getComputedStyle(document.documentElement);
        return {
          // a theme may give the charts their own printing and idle colours (--chart-*), and
          // printing its own colour (--st-printing); the rest take --info and the greys
          printing: style.getPropertyValue('--chart-printing').trim() || style.getPropertyValue('--st-printing').trim() || style.getPropertyValue('--info').trim(),
          finished: style.getPropertyValue('--chart-ok').trim() || style.getPropertyValue('--ok').trim(),
          paused: style.getPropertyValue('--chart-warn').trim() || style.getPropertyValue('--warn').trim(),
          error: style.getPropertyValue('--chart-danger').trim() || style.getPropertyValue('--danger').trim(),
          cancelled: style.getPropertyValue('--gray-9').trim(),
          idle: style.getPropertyValue('--chart-idle').trim() || style.getPropertyValue('--gray-11').trim(),
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