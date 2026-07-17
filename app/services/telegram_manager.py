"""Owns the Telegram reporter and proxy checker; hot-applies settings edits.

main() calls init() once; the module itself is passed to poll_loop as the
Notifier (it has notify()). The settings API calls apply_settings() after a
save: proxy changes go straight into the live ProxyChecker, bot-identity
changes (enabled/token/chat) restart the reporter thread in the background —
strictly sequentially, because two concurrent getUpdates long-polls with the
same token draw a 409 from Telegram.
"""
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from app.services import settings_service

logger = logging.getLogger(__name__)

# Reporter thread can sit in get_updates(timeout=20) before noticing stop.
_JOIN_TIMEOUT = 25.0

_lock = threading.Lock()
_reporter = None          # TelegramReporter | None
_proxy_checker = None     # ProxyChecker | None
_status_getter: Optional[Callable[[], str]] = None
_restarting = False


def _make_reporter():
    from telegram_bot_async import TelegramReporter

    s = settings_service.get_all()
    reporter = TelegramReporter(
        token=s["telegram_token"],
        allowed_chat_id=s["telegram_chat_id"],
        update_interval=s["telegram_update_interval"],
        proxy=_proxy_checker.best_proxy if _proxy_checker else None,
        proxy_getter=lambda: _proxy_checker.best_proxy if _proxy_checker else None,
    )
    if _status_getter is not None:
        reporter.set_status_getter(_status_getter)
    return reporter


def _start_bot_locked(wait_for_proxy: bool):
    """Create and start the reporter. Caller holds _lock."""
    global _reporter
    s = settings_service.get_all()
    if not (s["telegram_enabled"] and s["telegram_token"]):
        logger.info("Telegram bot disabled")
        return
    try:
        if _proxy_checker and wait_for_proxy:
            _proxy_checker.wait_ready()
        _reporter = _make_reporter()
        _reporter.start()
        logger.info("Telegram bot started")
    except Exception as e:
        logger.warning("Telegram bot failed to start: %s", e)
        _reporter = None


def init(status_getter: Callable[[], str]) -> None:
    """Startup: bring up the proxy checker and (in the background) the bot.

    The checker runs whenever the proxy list is non-empty — even with the bot
    disabled — so the settings page always shows live latencies. Bot startup
    waits for the first proxy check, so it runs off the main thread to keep
    service startup non-blocking.
    """
    global _proxy_checker, _status_getter
    _status_getter = status_getter
    s = settings_service.get_all()

    if s["proxy_list"]:
        try:
            from proxy_checker import ProxyChecker
            _proxy_checker = ProxyChecker(s["proxy_list"], s["proxy_check_interval"])
            _proxy_checker.start()
        except Exception as e:
            logger.warning("ProxyChecker failed to start: %s", e)
            _proxy_checker = None

    def _bg_start():
        with _lock:
            _start_bot_locked(wait_for_proxy=True)

    threading.Thread(target=_bg_start, daemon=True, name="tg-start").start()


def notify(text: str) -> bool:
    """Poll-loop Notifier entry point. Safe no-op while the bot is down.

    Returns True only when the message was actually dispatched (bot up and a
    chat established), so the caller can avoid recording a no-op as a real
    notification.
    """
    reporter = _reporter
    if reporter is not None:
        return bool(reporter.notify(text))
    return False


def _restart_bot_async():
    """Stop the current reporter, wait it out, start a fresh one."""
    global _reporter, _restarting

    def _work():
        global _reporter, _restarting
        try:
            with _lock:
                old = _reporter
                _reporter = None
                if old is not None:
                    old.stop(join_timeout=_JOIN_TIMEOUT)
                _start_bot_locked(wait_for_proxy=False)
        finally:
            _restarting = False

    _restarting = True
    threading.Thread(target=_work, daemon=True, name="tg-restart").start()


def apply_settings(changed_keys) -> Dict[str, Any]:
    """Hot-apply freshly saved settings. Returns {bot_restarting: bool}."""
    global _proxy_checker
    changed = set(changed_keys)
    s = settings_service.get_all()

    if changed & {"proxy_list", "proxy_check_interval"}:
        if s["proxy_list"] and _proxy_checker is None:
            try:
                from proxy_checker import ProxyChecker
                _proxy_checker = ProxyChecker(s["proxy_list"], s["proxy_check_interval"])
                _proxy_checker.start()
            except Exception as e:
                logger.warning("ProxyChecker failed to start: %s", e)
        elif _proxy_checker is not None:
            _proxy_checker.set_proxies(s["proxy_list"])
            _proxy_checker.set_interval(s["proxy_check_interval"])
            if "proxy_list" in changed:
                # Re-measure right away so the UI doesn't show stale dashes
                # until the next periodic cycle.
                threading.Thread(target=_proxy_checker.check_all, daemon=True,
                                 name="proxy-recheck").start()

    bot_restart = bool(changed & {"telegram_enabled", "telegram_token", "telegram_chat_id"})
    if bot_restart:
        _restart_bot_async()
    elif "telegram_update_interval" in changed and _reporter is not None:
        # Takes effect on the next periodic-update cycle, no restart needed.
        _reporter.update_interval = s["telegram_update_interval"]

    return {"bot_restarting": bot_restart}


def check_proxies_now() -> Dict[str, Optional[float]]:
    """Blocking re-check of every proxy (settings page button). ≤~10s."""
    if _proxy_checker is None:
        return {}
    return _proxy_checker.check_all()


def status() -> Dict[str, Any]:
    """Live state for the settings page."""
    s = settings_service.get_all()
    reporter = _reporter
    checker = _proxy_checker
    latencies = checker.latencies if checker else {}
    best = checker.best_proxy if checker else None
    proxies: List[Dict[str, Any]] = []
    for url in s["proxy_list"]:
        lat = latencies.get(url)
        proxies.append({
            "url": url,
            "online": lat is not None,
            "latency_ms": round(lat * 1000) if lat is not None else None,
            "is_best": url == best,
        })
    return {
        "enabled": s["telegram_enabled"],
        "running": bool(reporter and reporter.is_running),
        "restarting": _restarting,
        "chat_established": bool(reporter and reporter.chat_established),
        "proxies": proxies,
        "last_check": checker.last_check if checker else None,
    }


def reset_for_tests() -> None:
    global _reporter, _proxy_checker, _status_getter, _restarting
    with _lock:
        if _reporter is not None:
            _reporter.stop()
        if _proxy_checker is not None:
            _proxy_checker.stop()
        _reporter = None
        _proxy_checker = None
        _status_getter = None
        _restarting = False
