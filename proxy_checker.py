import asyncio
import logging
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def mask_proxy(url: Optional[str]) -> Optional[str]:
    """Proxy URL for logs: keep scheme://host:port, hide the login:password."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid proxy url>"
    if "@" not in parts.netloc:
        return url
    return "%s://***@%s" % (parts.scheme, parts.netloc.rsplit("@", 1)[1])


def _scrub(text: str, url: str) -> str:
    """Remove a proxy URL's credentials from free text such as exception messages."""
    if not url or "@" not in url:
        return text
    return text.replace(url, mask_proxy(url)).replace(urlsplit(url).netloc.rsplit("@", 1)[0] + "@", "***@")

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
            except Exception:
                logger.exception("Proxy check cycle failed")
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
