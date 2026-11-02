// The panel's languages: Русский (the source language, nothing to do) and the ones
// registered by app/web/i18n/<lang>.js. Served publicly at /i18n.js together with
// every app/web/i18n/*.js (app/api/i18n.py), loaded first by index.html and
// login.html, before theme.js and app.js.
//
// The translation is keyed by the Russian string, in the spirit of gettext: app.js
// and index.html keep their Russian text as it is, the DOM is translated after it
// is written:
//   * innerHTML / insertAdjacentHTML: at once, inside the setter (the app measures
//     what it has just drawn: chip rows, popovers, tooltips);
//   * everything else (textContent, attributes, nodes): a MutationObserver on the
//     document, before the next paint.
// A string is looked up trimmed, inner whitespace collapsed: the exact dictionary
// entry; else a pattern of the language (numbers, plurals, word order); else its
// " · " parts one by one; else the known pieces inside it, longest first, at word
// boundaries (the app glues texts from fragments). What stays Cyrillic after that
// is the user's own (printer and user names) or unknown (I18N.missing()).
// At the browser's API boundary: document.title, new Notification(),
// showNotification(), canvas fillText/strokeText/measureText, toLocale*('ru-RU')
// and localeCompare('ru') — so the app's call sites stay as they are.
// Nothing about the farm is here (the file is public): the dictionaries hold UI
// texts only; the fleet's live server texts (Bambu HMS) come from the
// session-gated /api/i18n/texts.
(function forgeI18n() {
  "use strict";
  var STORAGE_KEY = "forge-ops-lang";            // like forge-ops-theme, forge-ops-sort
  var SOURCE = "ru";
  // CIS languages: the Russian interface is the closer one for their speakers
  var RU_FAMILY = ["ru", "uk", "be", "kk", "ky", "uz", "tg", "hy", "az"];
  var FALLBACK = "en";
  var ATTRS = ["title", "placeholder", "aria-label", "alt", "data-tip", "data-l"];
  var ATTR_SELECTOR = ATTRS.map(function (a) { return "[" + a + "]"; }).join(",");
  // Never translated: the user's own text (names, logins, templates), code, the language names
  var SKIP_SELECTOR = "script,style,textarea,[translate=no],.notranslate," +
    ".pc-nm,.printer-name,.bd-name,.prk-name";
  var CYR = /[А-Яа-яЁё]/;
  var LETTER = /[A-Za-zА-Яа-яЁё]/;

  var langs = {};      // lang -> its registered data (+ the built lookup)
  langs[SOURCE] = { name: "Русский", locale: "ru-RU", strings: {} };
  var current = SOURCE;
  var active = null;   // the current language's data, null for Russian
  var cache = new Map();
  var textState = new WeakMap();   // Text -> {o: the app's text, s: what is shown}
  var attrState = new WeakMap();   // Element -> {name: {o, s}}
  var missing = new Set();
  var observer = null;
  var busy = false;
  var redo = false;    // refresh(): translate the shown nodes again from their originals
  // the cost of translating (I18N.stats()): batches, ms spent, nodes written
  var stats = { batches: 0, ms: 0, writes: 0, maxMs: 0 };
  var now = typeof performance !== "undefined" && performance.now ? function () { return performance.now(); } : Date.now;
  function spent(t0) {
    var d = now() - t0;
    stats.batches++;
    stats.ms += d;
    if (d > stats.maxMs) stats.maxMs = d;
  }

  function hasCyr(s) { return CYR.test(s); }
  function norm(s) { return String(s).replace(/ /g, " ").replace(/\s+/g, " ").trim(); }
  function swapFirst(s) {
    var c = s.charAt(0), u = c.toUpperCase();
    return (c === u ? c.toLowerCase() : u) + s.slice(1);
  }
  function isUpperFirst(s) { var c = s.charAt(0); return c !== c.toLowerCase(); }

  // ---- the lookup of one language -------------------------------------------------------

  function build(data) {
    var strings = data.strings || {};
    var trie = {};
    function put(key, val) {
      var n = trie;
      for (var i = 0; i < key.length; i++) n = n[key[i]] || (n[key[i]] = {});
      if (!("\u0000" in n)) n["\u0000"] = val;
    }
    var pats = [];
    Object.keys(strings).forEach(function (k) {
      if (!k || strings[k] == null) return;
      if (k.indexOf("%s") !== -1) { pats.push(fillPattern(k, strings[k])); return; }
      put(k, strings[k]);
    });
    // the same piece with its first letter's case swapped ("Пауза" inside "· пауза")
    Object.keys(strings).forEach(function (k) {
      var alt = swapFirst(k);
      if (alt !== k && !(alt in strings) && strings[k] != null) put(alt, swapFirst(strings[k]) === strings[k] ? strings[k] : caseLike(alt, strings[k]));
    });
    data._trie = trie;
    var all = (data.patterns || []).filter(function (p) { return p && p[0] instanceof RegExp; });
    data._patterns = all.filter(function (p) { return p[2] !== "sub"; }).concat(pats);
    data._subs = all.filter(function (p) { return p[2] === "sub"; });
    var ctx = {};
    (data.contexts || []).forEach(function (c) {
      Object.keys(c[1]).forEach(function (k) { (ctx[k] = ctx[k] || []).push([c[0], c[1][k]]); });
    });
    data._ctx = ctx;
  }
  // a key with %s (the app fills in a code): a pattern, the parts translated in place
  function fillPattern(key, val) {
    var re = new RegExp("^" + key.split("%s").map(function (x) {
      return x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }).join("(.+?)") + "$");
    var parts = val.split("%s");
    return [re, function (m, tr) {
      var out = parts[0];
      for (var i = 1; i < parts.length; i++) out += tr(m[i] || "") + parts[i];
      return out;
    }];
  }
  function caseLike(src, val) {
    if (!val) return val;
    return isUpperFirst(src) ? val.charAt(0).toUpperCase() + val.slice(1) : val.charAt(0).toLowerCase() + val.slice(1);
  }

  function exact(d, s) {
    var v = d.strings[s];
    if (v != null) return v;
    if (d.extra && d.extra[s] != null) return d.extra[s];
    var alt = swapFirst(s);
    v = d.strings[alt];
    if (v != null) return caseLike(s, v);
    return null;
  }

  function pieces(d, s) {
    var out = "", i = 0, n = s.length, trie = d._trie, changed = false;
    while (i < n) {
      var best = -1, val = null;
      if (i === 0 || !LETTER.test(s[i - 1]) || !LETTER.test(s[i])) {
        var node = trie, j = i;
        while (j < n && (node = node[s[j]])) {
          j++;
          if ("\u0000" in node && (j === n || !LETTER.test(s[j]) || !LETTER.test(s[j - 1]))) {
            best = j;
            val = node["\u0000"];
          }
        }
      }
      if (best > 0) {
        out += val;
        i = best;
        changed = true;
      } else {
        out += s[i];
        i++;
      }
    }
    return changed ? out : s;
  }

  function translate(d, s, depth) {
    var v = exact(d, s);
    if (v != null) return v;
    if (!hasCyr(s)) return s;
    var pats = d._patterns;
    for (var i = 0; i < pats.length; i++) {
      var m = pats[i][0].exec(s);
      if (!m || m[0] !== s) continue;
      var rep = pats[i][1];
      if (typeof rep === "function") return rep(m, function (x) { return tr(d, x, depth + 1); });
      return s.replace(pats[i][0], rep);
    }
    // the fleet's live texts (Bambu's HMS) inside a longer line: «Пауза-ошибка: 0300_8014 — <text>»
    if (d.extra) {
      for (var ek in d.extra) {
        if (ek.length > 12 && s.indexOf(ek) !== -1) s = s.split(ek).join(d.extra[ek]);
      }
      if (!hasCyr(s)) return s;
    }
    var whole = piecesOf(d, s, depth);
    if (depth < 4 && s.indexOf(" · ") > 0 && hasCyr(whole)) {
      var split = s.split(" · ").map(function (part, i) {
        var one = tr(d, part, depth + 1);
        if (i && hasCyr(one)) {
          // a piece that starts with its separator: «· поставил» + a name
          var sep = tr(d, "· " + part, depth + 1);
          if (sep.indexOf("· ") === 0 && cyrCount(sep) < cyrCount(one)) one = sep.slice(2);
        }
        return one;
      }).join(" · ");
      if (cyrCount(split) < cyrCount(whole)) return split;
    }
    return whole;
  }

  function cyrCount(s) { var m = s.match(/[А-Яа-яЁё]/g); return m ? m.length : 0; }
  // what the pieces make of a string; a text mostly left Russian is the user's own or
  // unknown (a name, a server text not in the dictionary): kept whole, not half-translated
  function piecesOf(d, s, depth) {
    var s0 = s, subs = d._subs;
    for (var k = 0; k < subs.length; k++) {
      var re = subs[k][0], sub = subs[k][1];
      re.lastIndex = 0;
      if (!re.test(s)) continue;
      re.lastIndex = 0;
      s = typeof sub === "function"
        ? s.replace(re, function () {
          var m = Array.prototype.slice.call(arguments, 0, -2);
          return sub(m, function (x) { return tr(d, x, depth + 1); });
        })
        : s.replace(re, sub);
    }
    var out = pieces(d, s);
    var cyr = cyrCount(out);
    if (cyr) {
      var letters = (out.match(/[A-Za-zА-Яа-яЁё]/g) || []).length;
      if (cyr * 10 > letters * 3) return s0;
    }
    return out;
  }

  // the translation of one string in language d, whitespace around it kept
  function tr(d, s, depth) {
    s = String(s);
    if (!hasCyr(s)) return s;
    var key = norm(s);
    var hit = depth ? undefined : cache.get(key);
    var out;
    if (hit !== undefined) out = hit;
    else {
      out = translate(d, key, depth || 0);
      if (!depth && d.fix) out = d.fix(out);
      if (!depth) {
        if (cache.size > 20000) cache.clear();
        cache.set(key, out);
        if (hasCyr(out)) noteMissing(key, out);
      }
    }
    if (out === key) return s;
    var lead = /^\s*/.exec(s)[0], trail = /\s*$/.exec(s)[0];
    return lead + out + trail;
  }

  // ---- the DOM --------------------------------------------------------------------------

  function skipped(el) {
    return !!(el && el.closest && el.closest(SKIP_SELECTOR));
  }

  // the context entry of the language for this element's place, or null
  function inContext(el, s) {
    var list = active._ctx[norm(s)];
    if (!list || !el || !el.closest) return null;
    for (var i = 0; i < list.length; i++) if (el.closest(list[i][0])) return list[i][1];
    return null;
  }

  function doText(node) {
    var data = node.data;
    var st = textState.get(node);
    if (st && data === st.s) {                     // ours, already translated
      if (!redo) return;
      data = st.o;                                 // the dictionary grew: again from the app's text
    }
    if (!active) {
      if (st && data === st.s) node.data = st.o;
      return;
    }
    if (!hasCyr(data)) { if (st) textState.delete(node); return; }
    if (skipped(node.parentElement)) return;
    var out = inContext(node.parentElement, data) || tr(active, data);
    if (out === node.data) return;
    textState.set(node, { o: data, s: out });
    stats.writes++;
    node.data = out;
  }

  function doAttr(el, name) {
    var v = el.getAttribute(name);
    if (v == null) return;
    var map = attrState.get(el), st = map && map[name];
    if (st && v === st.s) {
      if (!redo) return;
      v = st.o;
    }
    if (!hasCyr(v) || skipped(el)) return;
    var out = inContext(el, v) || tr(active, v);
    if (out === el.getAttribute(name)) return;
    if (!map) attrState.set(el, map = {});
    map[name] = { o: v, s: out };
    stats.writes++;
    el.setAttribute(name, out);
  }

  function doElementAttrs(el) {
    for (var i = 0; i < ATTRS.length; i++) if (el.hasAttribute(ATTRS[i])) doAttr(el, ATTRS[i]);
  }

  function doTree(root) {
    if (!active || !root) return;
    if (root.nodeType === 3) { doText(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 11 && root.nodeType !== 9) return;
    if (root.nodeType === 1 && skipped(root)) return;
    if (root.nodeType === 1) doElementAttrs(root);
    if (root.querySelectorAll) {
      var els = root.querySelectorAll(ATTR_SELECTOR);
      for (var i = 0; i < els.length; i++) doElementAttrs(els[i]);
    }
    var doc = root.ownerDocument || root;
    var walker = doc.createTreeWalker(root, 4 /* SHOW_TEXT */, null);
    var n, list = [];
    while ((n = walker.nextNode())) if (hasCyr(n.data) || (redo && textState.has(n))) list.push(n);
    for (var j = 0; j < list.length; j++) doText(list[j]);
  }

  // back to the app's own text (switching to Russian)
  function undoTree(root) {
    var walker = document.createTreeWalker(root, 4, null), n;
    while ((n = walker.nextNode())) {
      var st = textState.get(n);
      if (st && n.data === st.s) n.data = st.o;
      textState.delete(n);
    }
    var els = root.querySelectorAll(ATTR_SELECTOR);
    for (var i = 0; i < els.length; i++) {
      var map = attrState.get(els[i]);
      if (!map) continue;
      Object.keys(map).forEach(function (name) {
        if (els[i].getAttribute(name) === map[name].s) els[i].setAttribute(name, map[name].o);
      });
      attrState.delete(els[i]);
    }
  }

  function onMutations(records) {
    if (!active || busy) return;
    busy = true;
    var t0 = now();
    try {
      for (var i = 0; i < records.length; i++) {
        var r = records[i];
        if (r.type === "characterData") doText(r.target);
        else if (r.type === "attributes") { if (!skipped(r.target)) doAttr(r.target, r.attributeName); }
        else for (var j = 0; j < r.addedNodes.length; j++) {
          var a = r.addedNodes[j];
          if (a.isConnected) doTree(a);
        }
      }
    } finally {
      if (observer) observer.takeRecords();    // our own writes: nothing more to do
      busy = false;
      spent(t0);
    }
  }

  function observe() {
    if (observer || typeof MutationObserver !== "function") return;
    observer = new MutationObserver(onMutations);
    observer.observe(document, { subtree: true, childList: true, characterData: true,
                                 attributes: true, attributeFilter: ATTRS });
  }

  // innerHTML / insertAdjacentHTML: translated inside the setter, so whatever measures
  // the new markup right after it measures the translated text
  (function hookHtmlSetters() {
    if (typeof Element === "undefined") return;
    var desc = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
    if (desc && desc.set && desc.configurable) {
      Object.defineProperty(Element.prototype, "innerHTML", {
        configurable: true, enumerable: desc.enumerable, get: desc.get,
        set: function (v) {
          desc.set.call(this, v);
          if (active && !busy && hasCyr(String(v))) {
            busy = true;
            var t0 = now();
            try { doTree(this.content || this); } finally { busy = false; spent(t0); }
          }
        }
      });
    }
    var adj = Element.prototype.insertAdjacentHTML;
    if (adj) {
      Element.prototype.insertAdjacentHTML = function (pos, html) {
        var r = adj.call(this, pos, html);
        if (active && !busy && hasCyr(String(html))) {
          busy = true;
          try { doTree(/^(beforebegin|afterend)$/i.test(pos) && this.parentNode ? this.parentNode : this); } finally { busy = false; }
        }
        return r;
      };
    }
  })();

  // ---- the browser's API boundary -------------------------------------------------------

  function t(s) {
    if (!active || s == null) return s;
    return tr(active, s);
  }

  // the title app.js last set, shown again in the new language on a switch (applyLang)
  var retitle = function () {};

  (function hookTitle() {
    if (typeof document === "undefined" || typeof Document === "undefined") return;
    var desc = Object.getOwnPropertyDescriptor(Document.prototype, "title");
    if (!desc || !desc.set) return;
    var orig = null, shown = null;
    retitle = function () {
      if (orig === null) return;
      shown = t(orig);
      desc.set.call(document, shown);
    };
    try {
      Object.defineProperty(document, "title", {
        configurable: true,
        get: function () {
          var real = desc.get.call(document);
          return orig !== null && real === shown ? orig : real;
        },
        set: function (v) {
          orig = String(v);
          shown = t(orig);
          desc.set.call(document, shown);
        }
      });
    } catch (e) {}
  })();

  (function hookNotifications() {
    if (typeof window === "undefined") return;
    var N = window.Notification;
    if (typeof N === "function") {
      var TN = function Notification(title, opts) {
        var o = Object.assign({}, opts || {});
        if (o.body != null) o.body = t(o.body);
        return new N(t(title), o);
      };
      TN.prototype = N.prototype;
      Object.defineProperty(TN, "permission", { get: function () { return N.permission; } });
      if (N.requestPermission) TN.requestPermission = N.requestPermission.bind(N);
      if ("maxActions" in N) Object.defineProperty(TN, "maxActions", { get: function () { return N.maxActions; } });
      window.Notification = TN;
    }
    var SWR = window.ServiceWorkerRegistration;
    if (SWR && SWR.prototype.showNotification) {
      var show = SWR.prototype.showNotification;
      SWR.prototype.showNotification = function (title, opts) {
        var o = Object.assign({}, opts || {});
        if (o.body != null) o.body = t(o.body);
        return show.call(this, t(title), o);
      };
    }
  })();

  (function hookCanvas() {
    var C = typeof CanvasRenderingContext2D !== "undefined" && CanvasRenderingContext2D.prototype;
    if (!C) return;
    ["fillText", "strokeText", "measureText"].forEach(function (name) {
      var f = C[name];
      if (!f) return;
      C[name] = function (text) {
        if (active && typeof text === "string" && hasCyr(text)) {
          var args = Array.prototype.slice.call(arguments);
          args[0] = t(text);
          return f.apply(this, args);
        }
        return f.apply(this, arguments);
      };
    });
  })();

  // toLocale*("ru-RU") / localeCompare(x, "ru") -> the current language's locale
  function loc(l) {
    if (!active) return l;
    if (l === "ru" || l === "ru-RU" || (Array.isArray(l) && (l[0] === "ru" || l[0] === "ru-RU"))) return active.locale;
    return l;
  }
  (function hookLocales() {
    function wrap(proto, name, idx) {
      var f = proto && proto[name];
      if (!f) return;
      proto[name] = function () {
        if (active && arguments.length > idx) {
          var args = Array.prototype.slice.call(arguments);
          args[idx] = loc(args[idx]);
          return f.apply(this, args);
        }
        return f.apply(this, arguments);
      };
    }
    wrap(Date.prototype, "toLocaleString", 0);
    wrap(Date.prototype, "toLocaleDateString", 0);
    wrap(Date.prototype, "toLocaleTimeString", 0);
    wrap(Number.prototype, "toLocaleString", 0);
    wrap(String.prototype, "localeCompare", 1);
  })();

  // ---- the server's live texts (Bambu's HMS / print-error texts of the fleet) ---------------

  var serverAt = 0, serverTimer = null, serverBusy = false;
  var asked = new Set();     // sent once: names and the like stay unknown, they are not asked again
  function noteMissing(key) {
    if (missing.has(key)) return;
    if (missing.size < 2000) missing.add(key);
    if (asked.has(key)) return;
    if (typeof document === "undefined" || !document.getElementById("mainShell")) return;   // the login page
    if (serverTimer) return;
    var wait = Math.max(600, 3000 - (Date.now() - serverAt));   // a burst of new texts: one request
    serverTimer = setTimeout(loadServerTexts, wait);
  }
  function loadServerTexts() {
    serverTimer = null;
    if (!active || typeof fetch !== "function") return;
    if (serverBusy) { serverTimer = setTimeout(loadServerTexts, 1000); return; }
    serverBusy = true;
    serverAt = Date.now();
    var lang = current;
    var ask = Array.from(missing).filter(function (k) { return !asked.has(k); }).slice(0, 200);
    ask.forEach(function (k) { asked.add(k); });
    fetch("/api/i18n/texts?lang=" + encodeURIComponent(lang), {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texts: ask })
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (map) {
        if (!map || !active || current !== lang) return;
        var d = active, added = 0;
        d.extra = d.extra || {};
        Object.keys(map).forEach(function (k) {
          var key = norm(k);
          if (key && typeof map[k] === "string" && d.extra[key] !== map[k]) { d.extra[key] = map[k]; added++; }
        });
        if (added) {
          cache.clear();
          missing.clear();
          refresh();
        }
      })
      .catch(function () {})
      .then(function () {
        serverBusy = false;
        // what was met while the request ran
        var more = Array.from(missing).some(function (k) { return !asked.has(k); });
        if (more && !serverTimer) serverTimer = setTimeout(loadServerTexts, 3000);
      });
  }

  // ---- choosing the language ------------------------------------------------------------

  // navigator.languages -> the panel's language: the CIS languages read Russian, a
  // registered language its own, everything else the fallback (English)
  function detect(list) {
    list = (list && list.length ? list : ["en"]);
    for (var i = 0; i < list.length; i++) {
      var base = String(list[i] || "").toLowerCase().split(/[-_]/)[0];
      if (!base) continue;
      if (RU_FAMILY.indexOf(base) !== -1) return SOURCE;
      if (langs[base]) return base;
      for (var k in langs) if ((langs[k].match || []).indexOf(base) !== -1) return k;
    }
    return langs[FALLBACK] ? FALLBACK : SOURCE;
  }

  function stored() {
    try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
  }

  function initial() {
    var s = stored();
    if (s && langs[s]) return s;
    var nav = typeof navigator !== "undefined" ? (navigator.languages && navigator.languages.length
      ? navigator.languages : [navigator.language]) : [];
    return detect(nav);
  }

  function refresh() {
    if (!active || typeof document === "undefined") return;
    busy = true;
    redo = true;
    try { doTree(document); } finally { if (observer) observer.takeRecords(); busy = false; redo = false; }
  }

  function applyLang(lang, first) {
    if (!langs[lang]) lang = SOURCE;
    var was = current;
    current = lang;
    active = lang === SOURCE ? null : langs[lang];
    cache.clear();
    missing.clear();
    if (typeof document === "undefined") return;
    var root = document.documentElement;
    root.setAttribute("lang", lang);
    var man = document.querySelector('link[rel="manifest"]');
    if (man) {
      var href = lang === SOURCE ? "/manifest.webmanifest" : "/manifest." + lang + ".webmanifest";
      if (man.getAttribute("href") !== href) man.setAttribute("href", href);
    }
    if (!first && was !== SOURCE) {
      busy = true;
      try { undoTree(document); } finally { if (observer) observer.takeRecords(); busy = false; }
    }
    if (active) {
      refresh();
      var title = document.querySelector("title");
      if (title && hasCyr(title.textContent)) title.textContent = t(title.textContent);
    }
    syncSwitchers();
    relink(lang);
    if (!first) {
      retitle();
      // what draws itself (the timeline's canvas) follows on its next draw; a resize asks for it
      try { window.dispatchEvent(new Event("resize")); } catch (e) {}
      try { document.dispatchEvent(new CustomEvent("forge-lang", { detail: { lang: lang } })); } catch (e) {}
    }
  }

  // a link with a page per language (👤 → «Документация»): data-href-<lang>, else the source's
  function relink(lang) {
    var links = document.querySelectorAll("a[data-href-" + SOURCE + "]");
    for (var i = 0; i < links.length; i++) {
      var href = links[i].getAttribute("data-href-" + lang) || links[i].getAttribute("data-href-" + SOURCE);
      if (links[i].getAttribute("href") !== href) links[i].setAttribute("href", href);
    }
  }

  function setLang(lang) {
    if (!langs[lang]) return;
    try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) {}
    if (lang !== current) applyLang(lang, false);
  }

  // ---- the switchers: 👤 «Этот браузер» → Оформление, and the login page --------------------

  function switcherHtml(cls) {
    var html = '<span class="' + cls + '" role="group" aria-label="Язык">';
    Object.keys(langs).forEach(function (l) {
      html += '<button type="button" data-lang="' + l + '" aria-pressed="false" translate="no">' + langs[l].name + "</button>";
    });
    return html + "</span>";
  }

  function syncSwitchers() {
    if (typeof document === "undefined") return;
    var btns = document.querySelectorAll("button[data-lang]");
    for (var i = 0; i < btns.length; i++) {
      var on = btns[i].getAttribute("data-lang") === current;
      btns[i].classList.toggle("active", on);
      btns[i].setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  function mountSwitchers() {
    var theme = document.getElementById("themeSegment");
    if (theme && !document.getElementById("langRow")) {
      var row = document.createElement("div");
      row.className = "me-row";
      row.id = "langRow";
      row.innerHTML = "<span>Язык</span>" + switcherHtml("adm-seg i18n-seg");
      theme.parentNode.insertBefore(row, theme.nextSibling);
    }
    var form = document.getElementById("f");
    if (form && !document.getElementById("mainShell") && !document.getElementById("langRow")) {
      var st = document.createElement("style");
      st.textContent = ".i18n-login{display:flex;justify-content:center;gap:4px;margin-top:14px}" +
        ".i18n-login button{width:auto;margin:0;padding:4px 10px;border:1px solid transparent;border-radius:8px;" +
        "background:none;color:var(--text-muted);font:inherit;font-size:.75rem;font-weight:500;letter-spacing:0;cursor:pointer}" +
        ".i18n-login button:hover{color:var(--text)}" +
        ".i18n-login button.active{color:var(--text);border-color:var(--line-strong)}";
      document.head.appendChild(st);
      var box = document.createElement("div");
      box.id = "langRow";
      box.innerHTML = switcherHtml("i18n-login");
      form.parentNode.appendChild(box);
    }
    document.addEventListener("click", function (ev) {
      var b = ev.target && ev.target.closest && ev.target.closest("button[data-lang]");
      if (b) setLang(b.getAttribute("data-lang"));
    });
    syncSwitchers();
  }

  // the language's own CSS (texts of ::before content in styles.css)
  function cssOf(lang) {
    var d = langs[lang];
    return d && d.css ? d.css : "";
  }
  function mountCss() {
    var css = Object.keys(langs).map(function (l) {
      var c = cssOf(l);
      return c ? c.replace(/:lang\b/g, ':root[lang="' + l + '"]') : "";
    }).join("\n");
    if (!css) return;
    var st = document.createElement("style");
    st.id = "i18nCss";
    st.textContent = css;
    (document.head || document.documentElement).appendChild(st);
  }

  var started = false;
  function start() {
    if (started) return;
    started = true;
    if (typeof document === "undefined") return;
    applyLang(initial(), true);
    observe();
    var ready = function () {
      mountCss();
      mountSwitchers();
      relink(current);
      refresh();
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready);
    else ready();
  }

  var api = {
    SOURCE: SOURCE,
    STORAGE_KEY: STORAGE_KEY,
    RU_FAMILY: RU_FAMILY,
    // app/web/i18n/<lang>.js: I18N.register("en", {name, locale, match, strings, patterns, fix, css})
    register: function (lang, data) {
      data.strings = data.strings || {};
      build(data);
      langs[lang] = data;
    },
    detect: detect,
    t: t,
    tr: function (lang, s) { return langs[lang] && lang !== SOURCE ? tr(langs[lang], s) : s; },
    lang: function () { return current; },
    locale: function () { return (active || langs[SOURCE]).locale; },
    languages: function () { return Object.keys(langs); },
    set: setLang,
    start: start,
    apply: function (root) { if (active) { busy = true; try { doTree(root); } finally { busy = false; } } },
    missing: function () { return Array.from(missing); },
    stats: function () { return Object.assign({}, stats); }
  };
  if (typeof window !== "undefined") window.I18N = api;
  else if (typeof globalThis !== "undefined") globalThis.I18N = api;
})();
