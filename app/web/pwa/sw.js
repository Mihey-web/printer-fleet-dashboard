/* Service worker of «Кузница»: served at /sw.js (scope /) by app/api/main.py.
 *
 * What it does: GET /static/* and the PWA icons come from the cache at once
 * and are refreshed in the background (stale-while-revalidate). Everything
 * else goes to the network untouched: navigations (/ is the login page or the
 * app shell, the server decides), /api/*, this file, the manifest, other
 * origins (Google Fonts). Only a 200 same-origin answer is stored, so a 401 of
 * an expired session or a 404 never lands in the cache.
 *
 * Deploys: asset URLs carry ?v=N and index.html is served no-store, so a bump
 * means new URLs; storing a URL drops the entries of the same path with
 * another query, old versions do not pile up. CACHE changes only when the
 * logic of this file changes, never on an asset bump.
 *
 * Kill switch: if this worker ever misbehaves, deploy a sw.js whose activate
 * handler deletes every "forge-*" cache, calls self.registration.unregister()
 * and does nothing else (no fetch handler). The browser fetches it on the next
 * navigation (updateViaCache "none" + Cache-Control no-cache on /sw.js); the
 * navigation after that runs without a worker. A page opened with ?nosw in
 * the URL unregisters the worker and clears the caches too (app.js).
 */
"use strict";

var CACHE = "forge-static-v1";

/* sw-route:begin */
// How the worker treats a request: "swr" (answer from the cache, refresh in
// the background) or null (no respondWith: the request goes to the network).
var FORGE_SW_ICONS = ["/pwa/icon-192.png", "/pwa/icon-512.png", "/pwa/icon-maskable-512.png"];
function forgeSwRoute(url, method, mode, swOrigin) {
  if (method !== "GET") return null;
  if (mode === "navigate") return null;
  var u;
  try {
    u = new URL(url, swOrigin);
  } catch (e) {
    return null;
  }
  if (u.origin !== swOrigin) return null;
  var path = u.pathname;
  if (path === "/api" || path.indexOf("/api/") === 0) return null;
  if (path === "/sw.js" || path === "/manifest.webmanifest") return null;
  if (path.indexOf("/static/") === 0) return "swr";
  if (FORGE_SW_ICONS.indexOf(path) !== -1) return "swr";
  return null;
}
/* sw-route:end */

self.addEventListener("install", function () {
  // No precache: the worker does not know the current ?v= of the assets.
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) {
      return k.indexOf("forge-") === 0 && k !== CACHE;
    }).map(function (k) {
      return caches.delete(k);
    }));
  }).then(function () {
    return self.clients.claim();
  }));
});

// Store a response, first dropping the entries of the same path with another
// query (app.js?v=95 when app.js?v=96 arrives).
function forgePut(cache, request, response) {
  var url = new URL(request.url);
  return cache.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) {
      var u = new URL(k.url);
      return u.pathname === url.pathname && u.search !== url.search;
    }).map(function (k) {
      return cache.delete(k);
    }));
  }).then(function () {
    return cache.put(request, response);
  });
}

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (forgeSwRoute(req.url, req.method, req.mode, self.location.origin) !== "swr") return;
  // Always revalidate: /static has ETag/Last-Modified but no Cache-Control, so
  // a default fetch could be answered from the heuristic HTTP cache without
  // asking the server and the stale copy would be stored again. no-cache makes
  // it a conditional GET — usually a cheap 304 — so a stale entry is fixed on
  // the next load.
  var net = fetch(req, { cache: "no-cache" });
  // Registered first, so the clone is taken before the page reads the body.
  var refresh = net.then(function (res) {
    if (!(res.ok && res.type === "basic")) return undefined;
    var copy = res.clone();
    return caches.open(CACHE).then(function (cache) {
      return forgePut(cache, req, copy);
    });
  }).catch(function () {});
  event.waitUntil(refresh);
  event.respondWith(caches.open(CACHE).then(function (cache) {
    return cache.match(req);
  }).catch(function () {
    return undefined;
  }).then(function (hit) {
    // Nothing cached: the network answer, or its error, goes to the page as is.
    return hit || net;
  }));
});

self.addEventListener("message", function (event) {
  // Sent on logout: an anonymous visitor of this device gets no cached assets.
  if (event.data && event.data.type === "forge-clear") event.waitUntil(caches.delete(CACHE));
});
