import asyncio
import base64
import logging
import re
import threading
import time
from typing import Optional
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

# Plain string handling on purpose: urlsplit raises on malformed URLs (an
# unbalanced IPv6 bracket) and misplaces the userinfo when the password has an
# unencoded '/', '?' or '#' — both used to leak the password into the log.
# Credentials end at the LAST '@' so a raw '@' inside the password is hidden too.
# Nothing here may raise: these helpers run inside except blocks and log filters.

_SCHEME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*\Z")


def _split_credentials(url):
    """(prefix, userinfo, host_part) of a proxy URL; userinfo is None without '@'."""
    if not isinstance(url, str):
        return "", None, ""
    scheme, sep, rest = url.partition("://")
    # Only a real scheme counts: in "user:pa://ss@host" the '://' is inside the password.
    if not sep or not _SCHEME_RE.match(scheme):
        prefix, rest = "", url
    else:
        prefix = scheme + sep
    if "@" not in rest:
        return prefix, None, rest
    userinfo, host = rest.rsplit("@", 1)
    return prefix, userinfo, host


def mask_proxy(url: Optional[str]) -> Optional[str]:
    """Proxy URL for logs: keep scheme://host:port, hide the login:password."""
    if not url:
        return url
    if not isinstance(url, str):
        return "<proxy>"
    prefix, userinfo, host = _split_credentials(url)
    if userinfo is None:
        return url
    return "%s***@%s" % (prefix, host)


def _secret_parts(url):
    """(plain_pairs, bare_secrets) hiding one proxy URL's credentials.

    plain_pairs are replaced anywhere: the URL itself, "login:password@" and the
    base64 "login:password" of a Basic auth header. bare_secrets (the bare
    userinfo/password and the password's pieces around a raw '/', '?', '#', '@'
    or whitespace, which urlsplit/yarl errors quote) are replaced only as whole
    words, so a password like "proxy/check" does not blank the word "proxy" in
    every log line.
    """
    _, userinfo, _ = _split_credentials(url)
    if not userinfo:
        return set(), set()
    variants = {userinfo, unquote(userinfo)}
    plain = {(url, mask_proxy(url))} | {(s + "@", "***@") for s in variants}
    for s in variants:
        for enc in ("utf-8", "latin-1"):
            try:
                b64 = base64.b64encode(s.encode(enc)).decode("ascii")
            except Exception:
                continue
            if len(b64) >= 8:
                plain.add((b64, "***"))
    # Percent-encoded spellings a library may print instead of the configured
    # one: fully quote()'d userinfo/password/URL, in both hex cases.
    decoded_pw = unquote(userinfo).partition(":")[2]
    for t in (userinfo, unquote(userinfo), decoded_pw, url):
        for f in (quote(t, safe=""), quote(t, safe=":@/")):
            for g in (_HEX_RE.sub(lambda m: m.group(0).upper(), f),
                      _HEX_RE.sub(lambda m: m.group(0).lower(), f)):
                if "%" in g and len(g) >= 6:
                    plain.add((g, "***"))
    bare = {s for s in variants if _not_a_word(s)}
    for v in variants:
        pw = v.partition(":")[2]
        if _not_a_word(pw):
            bare.add(pw)
        # Pieces around a raw '/', '?', '#', '@' or whitespace (urlsplit/yarl
        # errors quote them).
        bare.update(p for p in re.split(r"[/?#@\s]", pw) if _not_a_word(p))
    return plain, bare


_HEX_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def _not_a_word(s: str) -> bool:
    """True when s cannot be an ordinary word or number of a log line.

    Weak passwords like "proxy" or "8080" are still hidden in the URL,
    "login:password@" and base64 forms; they are just not blanked as free words.
    """
    if len(s) < 4:
        return False
    mixed = any(c.isalpha() for c in s) and any(c.isdigit() for c in s)
    return len(s) >= 8 or (len(s) >= 5 and mixed) or not s.isalnum()


def _compile(plain, bare):
    """Longest-first plain pairs and one whole-word regex for the bare secrets."""
    ordered = tuple(sorted(plain, key=lambda p: len(p[0]), reverse=True))
    rx = None
    if bare:
        alt = "|".join(re.escape(s) for s in sorted(bare, key=len, reverse=True))
        # A secret right after a percent escape ("%2FfakePW") still counts as
        # starting a word.
        rx = re.compile(r"(?:(?<![A-Za-z0-9_])|(?<=%[0-9A-Fa-f]{2}))(?:" + alt + r")(?![A-Za-z0-9_])")
    return ordered, rx


def _apply(text, compiled):
    ordered, rx = compiled
    for s, repl in ordered:
        if s in text:
            text = text.replace(s, repl)
    if rx is not None:
        text = rx.sub("***", text)
    return text


def _compile_for(urls):
    plain, bare = set(), set()
    for url in urls or ():
        p, b = _secret_parts(url)
        plain |= p
        bare |= b
    return _compile(plain, bare)


def scrub_secrets(text: str, urls) -> str:
    """Remove the credentials of every given proxy URL from free text.

    All secrets go in one longest-first pass, so a password that is a prefix of
    another proxy's password cannot leave the longer one's tail behind.
    """
    if not text or not isinstance(text, str):
        return text
    return _apply(text, _compile_for(urls))


def _scrub(text: str, url: str) -> str:
    """Remove a proxy URL's credentials from free text such as exception messages."""
    return scrub_secrets(text, [url])


# Proxy URLs the app has configured: the log filter scrubs their exact secrets,
# which also covers messages the generic URL pattern cannot recognise (quotes,
# whitespace, fragments without "scheme://"). Bounded and compiled once per
# change, so the per-record cost does not grow with every proxy edit.
_KNOWN_MAX = 64
_known_lock = threading.Lock()
_known_proxies: dict = {}  # url -> None, oldest first
_known_compiled = ((), None)


def register_proxies(urls) -> None:
    global _known_compiled
    batch = [u for u in urls or () if isinstance(u, str) and "@" in u]
    with _known_lock:
        for u in batch:
            _known_proxies.pop(u, None)  # re-insert: newest end
            _known_proxies[u] = None
        # Evict the oldest URLs, but never one from the batch in use right now.
        current = set(batch)
        while len(_known_proxies) > max(_KNOWN_MAX, len(current)):
            victim = next((k for k in _known_proxies if k not in current), None)
            if victim is None:
                break
            _known_proxies.pop(victim)
        _known_compiled = _compile_for(list(_known_proxies))


def known_proxies() -> list:
    with _known_lock:
        return list(_known_proxies)


_TOKEN_RE = re.compile(r'[^\s"<>]+')
# Also the dict-repr form: {'Proxy-Authorization': 'Basic ...'}.
_AUTH_HEADER_RE = re.compile(r"""(?i)((?:proxy-)?authorization['"]?\s*[:=]\s*['"]?(?:basic|bearer)\s+)[^\s|"',;}]+""")


def _redact_token(m) -> str:
    tok = m.group(0)
    i = tok.find("://")
    j = tok.rfind("@")
    if i <= 0 or j <= i + 3:
        return tok
    # The scheme is the run of scheme characters right before '://' (linear scan,
    # so pathological lines cannot make this quadratic).
    k = i
    while k > 0 and tok[k - 1] in _SCHEME_CHARS:
        k -= 1
    while k < i and not tok[k].isalpha():
        k += 1
    if k == i:
        return tok
    return tok[:i + 3] + "***" + tok[j:]


def redact_credentials(text: str) -> str:
    """Mask login:password of every scheme://...@ URL and of auth headers in free text.

    Masks up to the last '@' of a whitespace-delimited run: over-masking an
    '@' in a path is an accepted trade-off for never showing a password.
    """
    if not text or not isinstance(text, str):
        return text
    if "uthorization" in text or "UTHORIZATION" in text:
        text = _AUTH_HEADER_RE.sub(r"\1***", text)
    if "://" not in text or "@" not in text:
        return text
    return _TOKEN_RE.sub(_redact_token, text)


def redact_text(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    return redact_credentials(_apply(text, _known_compiled))


def _safe(fn, value, fallback):
    try:
        return fn(value)
    except Exception:
        return fallback


class RedactingFilter(logging.Filter):
    """Handler filter that strips proxy credentials from messages and tracebacks.

    Works with any formatter (root and uvicorn handlers alike): the message is
    pre-rendered and the traceback pre-formatted, both redacted. Never drops a
    record and never raises; a record whose args do not fit its format is turned
    into a plain message, so logging's handleError never prints the raw args.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            try:
                msg = record.getMessage()
            except Exception:
                args = record.args if isinstance(record.args, tuple) else (record.args,)
                # Keep the call site: handleError used to print it, now it never runs.
                msg = "%s (bad log args: %s) [%s:%s]" % (
                    _safe(str, record.msg, "<unprintable>"),
                    ", ".join(_safe(repr, a, "<unrepr-able>") for a in args),
                    record.pathname, record.lineno)
            # uvicorn's colourised formatter re-renders color_message with the
            # args; pre-render it too, or the console shows "[%d]" templates.
            cm = record.__dict__.get("color_message")
            if isinstance(cm, str):
                try:
                    record.color_message = redact_text(cm % record.args if record.args else cm)
                except Exception:
                    record.__dict__.pop("color_message", None)
            record.msg, record.args = redact_text(msg), None
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact_text(record.exc_text)
            if record.stack_info:
                record.stack_info = redact_text(record.stack_info)
        except Exception:
            try:
                record.msg = "<unformattable log record %s:%s>" % (record.pathname, record.lineno)
                record.args = None
                record.exc_info = record.exc_text = None
            except Exception:
                pass
        return True

CHECK_URL = "https://api.telegram.org"
CHECK_TIMEOUT = 10


class ProxyChecker:
    """Periodically checks HTTP proxy connectivity and latency.

    Runs a background thread with its own asyncio event loop. Every
    ``check_interval`` seconds, pings each proxy via an HTTP request to
    ``CHECK_URL`` and selects the one with the lowest latency.

    Thread-safe: ``best_proxy`` can be read from any thread.
    """

    def __init__(self, proxies: list[str], check_interval: int = 600):
        self._proxies = list(proxies)
        register_proxies(self._proxies)
        self._check_interval = check_interval
        self._lock = threading.Lock()
        self._best_proxy: Optional[str] = None
        self._latencies: dict[str, Optional[float]] = {}
        self._last_check: Optional[float] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()

    @property
    def best_proxy(self) -> Optional[str]:
        with self._lock:
            return self._best_proxy

    @property
    def latencies(self) -> dict[str, Optional[float]]:
        with self._lock:
            return dict(self._latencies)

    @property
    def last_check(self) -> Optional[float]:
        with self._lock:
            return self._last_check

    @property
    def proxies(self) -> list[str]:
        with self._lock:
            return list(self._proxies)

    def set_proxies(self, proxies: list[str]):
        """Replace the proxy list; takes effect on the next check cycle."""
        register_proxies(proxies)
        with self._lock:
            self._proxies = list(proxies)
            # Drop latencies of removed proxies; a vanished best is replaced
            # on the next _update().
            self._latencies = {p: self._latencies.get(p) for p in self._proxies}
            if self._best_proxy not in self._proxies:
                self._best_proxy = None

    def set_interval(self, seconds: int):
        with self._lock:
            self._check_interval = seconds

    def wait_ready(self, timeout: float = 30.0):
        self._ready_event.wait(timeout)

    async def _check_one(self, proxy_url: str) -> Optional[float]:
        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp not available for proxy check")
            return None
        start = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=CHECK_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(CHECK_URL, proxy=proxy_url) as resp:
                    if resp.status < 500:
                        elapsed = time.monotonic() - start
                        logger.debug("Proxy check OK: %s, %.3fs", mask_proxy(proxy_url), elapsed)
                        return elapsed
                    else:
                        logger.debug("Proxy %s returned HTTP %s", mask_proxy(proxy_url), resp.status)
                        return None
        except Exception as e:
            logger.debug("Proxy check failed %s: %s", mask_proxy(proxy_url), _scrub(str(e), proxy_url))
            return None

    async def _check_all_async(self) -> dict[str, Optional[float]]:
        proxies = self.proxies  # snapshot: the list can be edited concurrently
        tasks = [self._check_one(p) for p in proxies]
        results = await asyncio.gather(*tasks)
        return dict(zip(proxies, results))

    def check_all(self) -> dict[str, Optional[float]]:
        results = asyncio.run(self._check_all_async())
        self._update(results)
        return results

    def _update(self, results: dict[str, Optional[float]]):
        with self._lock:
            self._latencies = dict(results)
            self._last_check = time.time()
            best = None
            best_latency = float("inf")
            for proxy, latency in results.items():
                if latency is not None and latency < best_latency:
                    best_latency = latency
                    best = proxy
            if best != self._best_proxy:
                if best:
                    logger.info(
                        "Best proxy changed: %s -> %s (%.3fs)",
                        mask_proxy(self._best_proxy),
                        mask_proxy(best),
                        best_latency,
                    )
                elif self._best_proxy and not best:
                    logger.warning("All proxies failed, keeping current: %s", mask_proxy(self._best_proxy))
                    return
                self._best_proxy = best
        self._ready_event.set()

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._periodic_check())
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _periodic_check(self):
        while not self._stop_event.is_set():
            try:
                results = await self._check_all_async()
                self._update(results)
                online = sum(1 for v in results.values() if v is not None)
                logger.info("Proxy check: %d/%d online", online, len(results))
                if online > 0:
                    best_lat = min(v for v in results.values() if v is not None)
                    logger.info("Best proxy latency: %.3fs", best_lat)
            except Exception as e:
                # No traceback: chained aiohttp errors may quote a proxy URL with its
                # password. Building the message must not fail either, or the
                # checker thread dies and threading.excepthook prints it raw.
                try:
                    msg = redact_credentials(scrub_secrets(str(e), self.proxies))
                except Exception:
                    msg = "<unprintable error>"
                logger.error("Proxy check cycle failed: %s: %s", type(e).__name__, msg)
            await asyncio.sleep(self._check_interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("ProxyChecker started with %d proxies, interval=%ds", len(self._proxies), self._check_interval)

    def stop(self):
        self._stop_event.set()
        logger.debug("ProxyChecker stopping")
