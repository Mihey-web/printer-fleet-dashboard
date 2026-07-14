import threading
import asyncio
import logging
import time
from typing import Callable, Optional, Dict

logger = logging.getLogger(__name__)


class TelegramReporter:
    """Telegram reporter using aiogram (async).

    Starts aiogram polling in a background thread. Responds to /start by sending
    a persistent message and periodically editing it with the text returned by
    the status getter (sync callable).
    """

    def __init__(self, token: str, allowed_chat_id: Optional[int] = None, update_interval: int = 5, proxy: Optional[str] = None, proxy_getter: Optional[Callable[[], Optional[str]]] = None):
        self.token = token
        self.allowed_chat_id = allowed_chat_id
        self.update_interval = update_interval
        self.proxy = proxy
        self._proxy_getter = proxy_getter
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._get_text: Optional[Callable[[], str]] = None
        self._chat_id: Optional[int] = None
        self._message_id: Optional[int] = None
        self._periodic_task = None
        self._available = True
        self._last_text: Optional[str] = None
        # Per-chat cooldown (timestamp until which edits should be skipped)
        self._cooldowns: Dict[int, float] = {}

        # Lazy import check � do not raise, degrade gracefully
        try:
            import aiogram  # noqa: F401
        except Exception as e:
            logger.warning("aiogram not available, TelegramReporter disabled: %s", e)
            self._available = False

    def set_status_getter(self, fn: Callable[[], str]):
        self._get_text = fn

    def notify(self, text: str):
        """Send a one-shot message to the current chat.

        Works only after a chat has been established via /start.
        Safe to call from any thread.
        """
        if not self._available:
            return
        if not self._chat_id:
            # Chat not known yet; user hasn't issued /start
            return
        try:
            # Best-effort fire-and-forget in a short-lived loop.
            async def _send_once():
                import importlib
                Bot = None
                candidates = ['aiogram', 'aiogram.client.bot', 'aiogram.bot']
                for mod in candidates:
                    try:
                        m = importlib.import_module(mod)
                        Bot = getattr(m, 'Bot', None)
                        if Bot:
                            break
                    except Exception:
                        continue
                if Bot is None:
                    logger.warning("aiogram Bot class not found; notify skipped")
                    return

                effective_proxy = self._get_effective_proxy()
                if effective_proxy:
                    from aiogram.client.session.aiohttp import AiohttpSession
                    session = AiohttpSession(proxy=effective_proxy)
                    bot = Bot(token=self.token, session=session)
                else:
                    bot = Bot(token=self.token)
                try:
                    await bot.send_message(chat_id=self._chat_id, text=text, parse_mode='HTML')
                finally:
                    try:
                        await bot.session.close()
                    except Exception:
                        pass

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_send_once())
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Telegram notify failed: %s", e)

    def start(self):
        if not self._available:
            logger.debug("TelegramReporter not available; start skipped")
            return
        if self._thread and self._thread.is_alive():
            return
        # Settings come in explicitly via the constructor (telegram_manager owns
        # them); the proxy getter keeps the effective proxy current at runtime.
        if self._proxy_getter is not None:
            try:
                dynamic = self._proxy_getter()
                if dynamic:
                    self.proxy = dynamic
            except Exception:
                pass

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("TelegramReporter thread started")

    def stop(self, join_timeout: float = 0.0):
        """Signal the polling thread to exit.

        The long-poll get_updates(timeout=20) means the thread can take up to
        ~20s to notice. Pass join_timeout to wait for it — required before
        starting a replacement reporter with the same token (Telegram returns
        409 for concurrent getUpdates).
        """
        if not self._available:
            return
        self._stop_event.set()
        logger.debug("TelegramReporter stopping")
        if join_timeout > 0 and self._thread and self._thread.is_alive():
            self._thread.join(join_timeout)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def chat_established(self) -> bool:
        return self._chat_id is not None

    def _get_effective_proxy(self) -> Optional[str]:
        if self._proxy_getter is not None:
            dynamic = self._proxy_getter()
            if dynamic:
                return dynamic
        return self.proxy

    def _run(self):
        try:
            import importlib
            Bot = None
            candidates = ['aiogram', 'aiogram.client.bot', 'aiogram.bot']
            for mod in candidates:
                try:
                    m = importlib.import_module(mod)
                    Bot = getattr(m, 'Bot', None)
                    if Bot:
                        break
                except Exception:
                    continue
            if Bot is None:
                logger.warning("aiogram Bot class not found; TelegramReporter stopped")
                return
        except Exception as e:
            logger.warning("Failed to locate aiogram Bot: %s", e)
            return

        async def poll_loop():
            offset = None
            retry_delay = 1
            max_retry_delay = 30
            current_proxy = self.proxy

            bot = None
            async def ensure_bot():
                nonlocal bot, current_proxy
                new_proxy = self._get_effective_proxy()
                if bot is not None and new_proxy == current_proxy:
                    return
                if bot is not None:
                    try:
                        await bot.session.close()
                    except Exception:
                        pass
                current_proxy = new_proxy
                if current_proxy:
                    from aiogram.client.session.aiohttp import AiohttpSession
                    session = AiohttpSession(proxy=current_proxy)
                    bot = Bot(token=self.token, session=session)
                    logger.info("Telegram bot using proxy: %s", current_proxy)
                else:
                    bot = Bot(token=self.token)
                    logger.info("Telegram bot: direct connection (no proxy)")
                logger.info("Telegram bot instance created, starting polling...")

            await ensure_bot()

            # Proactively establish the chat if we already know it from settings.
            # Telegram only forbids messaging a user who never /start-ed the bot
            # *ever*; once that happened, allowed_chat_id lets us resume after any
            # restart without a fresh /start. Best-effort: if it fails (user truly
            # never started the bot, network), fall through to the /start handler.
            if self.allowed_chat_id is not None and self._chat_id is None:
                await self._establish_chat(bot, self.allowed_chat_id)

            while not self._stop_event.is_set():
                try:
                    new_proxy = self._get_effective_proxy()
                    if new_proxy != current_proxy:
                        logger.info("Proxy changed: %s -> %s", current_proxy, new_proxy)
                        await ensure_bot()

                    updates = await bot.get_updates(offset=offset, timeout=20)
                    retry_delay = 1

                    for upd in updates:
                        try:
                            offset = upd.update_id + 1
                        except Exception:
                            offset = None
                        msg = getattr(upd, 'message', None)
                        if not msg:
                            continue
                        text = getattr(msg, 'text', '')
                        if not text:
                            continue
                        if text.split()[0] == '/start':
                            if not msg.chat:
                                continue
                            chat_id = msg.chat.id
                            if self.allowed_chat_id is None or chat_id != self.allowed_chat_id:
                                logger.warning("Denied chat %s", chat_id)
                                continue
                            # Re-send a fresh status message on every explicit /start,
                            # even if the chat was already auto-established at startup,
                            # so the user gets a new live message to look at.
                            await self._establish_chat(bot, chat_id, force=True)
                except Exception as e:
                    logger.warning("Polling failed: %s. Retrying in %d sec...", e, retry_delay)
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(poll_loop())
        except Exception:
            logger.exception("aiogram polling failed")
        finally:
            try:
                loop.run_until_complete(bot.session.close())
            except Exception:
                pass

    async def _establish_chat(self, bot, chat_id: int, force: bool = False) -> bool:
        """Send the initial status message and kick off periodic updates.

        Called both from the /start handler and proactively at startup (when
        allowed_chat_id is already known). Returns True on success. Best-effort:
        on failure it logs and returns False so the caller can keep polling —
        e.g. the very first time the user has genuinely never started the bot,
        Telegram rejects the send and only a real /start can bootstrap the chat.
        """
        if self._chat_id is not None and not force:
            return True
        content = "" if not self._get_text else self._get_text()
        try:
            sent = await bot.send_message(chat_id=chat_id, text=content or "(no data)", parse_mode='HTML')
        except Exception as e:
            logger.warning("Could not establish Telegram chat %s: %s", chat_id, e)
            return False
        self._chat_id = chat_id
        self._message_id = sent.message_id
        self._last_text = content or "(no data)"
        logger.info("Telegram chat established (chat_id=%s), initial message length=%d", chat_id, len(self._last_text))
        if self._periodic_task is None:
            self._periodic_task = asyncio.create_task(self._periodic_update(bot))
        return True

    async def _periodic_update(self, bot):
        while not self._stop_event.is_set():
            await asyncio.sleep(self.update_interval)
            if not self._chat_id or not self._message_id or not self._get_text:
                continue

            # If a cooldown is active for this chat, skip attempting edits until it expires
            cooldown_until = self._cooldowns.get(self._chat_id)
            now = time.time()
            if cooldown_until and now < cooldown_until:
                # skip edit attempts to avoid hitting rate limits repeatedly
                logger.debug("Skipping Telegram edit for chat %s until %s (%.1f seconds left)",
                             self._chat_id, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(cooldown_until)),
                             cooldown_until - now)
                continue

            try:
                text = self._get_text() or ""
                # Debug info
                preview = text[:200].replace('\n', '\\n')
                logger.debug("Telegram periodic update: len=%d preview=%s", len(text), preview)
                # Skip edit if content unchanged
                if text == self._last_text:
                    logger.debug("Telegram update skipped: content unchanged")
                    continue
                logger.debug("Editing Telegram message (len=%d)", len(text))
                await bot.edit_message_text(chat_id=self._chat_id, message_id=self._message_id, text=text, parse_mode='HTML')
                self._last_text = text
                # Clear any cooldown on success
                if self._chat_id in self._cooldowns:
                    del self._cooldowns[self._chat_id]
            except Exception as e:
                # Handle Telegram 'message is not modified' without spamming, otherwise log
                msg = str(e)

                # Network timeouts / transient network errors: enter a short cooldown to avoid log spam
                try:
                    from aiogram.exceptions import TelegramNetworkError
                except Exception:
                    TelegramNetworkError = None

                if TelegramNetworkError is not None and isinstance(e, TelegramNetworkError):
                    cooldown_until = time.time() + 30
                    self._cooldowns[self._chat_id] = cooldown_until
                    logger.warning(
                        "Telegram network error; entering cooldown for %ss: %s",
                        30,
                        msg,
                    )
                    continue

                # asyncio.TimeoutError / aiohttp timeouts may bubble up; treat similarly
                try:
                    import asyncio as _asyncio
                    if isinstance(e, _asyncio.TimeoutError):
                        cooldown_until = time.time() + 30
                        self._cooldowns[self._chat_id] = cooldown_until
                        logger.warning("Telegram request timeout; entering cooldown for %ss", 30)
                        continue
                except Exception:
                    pass

                # aiohttp ClientConnectorError (cannot connect to Telegram API)
                try:
                    from aiohttp import ClientConnectorError
                    if isinstance(e, ClientConnectorError):
                        cooldown_until = time.time() + 30
                        self._cooldowns[self._chat_id] = cooldown_until
                        logger.warning("Telegram connection error; entering cooldown for %ss: %s", 30, msg)
                        continue
                except Exception:
                    pass

                # Try to detect aiogram's TelegramRetryAfter exception and wait the required time
                try:
                    from aiogram.exceptions import TelegramRetryAfter
                except Exception:
                    TelegramRetryAfter = None

                handled = False

                # If it's an instance of TelegramRetryAfter, set cooldown for this chat
                if TelegramRetryAfter is not None and isinstance(e, TelegramRetryAfter):
                    wait = getattr(e, 'retry_after', None)
                    if wait is None:
                        # fallback: try to parse number from message
                        try:
                            import re
                            m = re.search(r'retry after (\d+)', msg, re.IGNORECASE)
                            if m:
                                wait = int(m.group(1))
                        except Exception:
                            wait = None
                    if wait:
                        cooldown_until = time.time() + wait
                        self._cooldowns[self._chat_id] = cooldown_until
                        logger.warning("Telegram rate limit hit for chat %s, entering cooldown for %s seconds", self._chat_id, wait)
                        # No need to await here; skip further handling until next loop iteration
                        handled = True

                # fallback: parse textual retry hint like 'retry after N'
                if not handled:
                    try:
                        import re
                        m = re.search(r'retry after (\d+)', msg, re.IGNORECASE)
                        if m:
                            wait = int(m.group(1))
                            cooldown_until = time.time() + wait
                            self._cooldowns[self._chat_id] = cooldown_until
                            logger.warning("Telegram rate limit detected for chat %s, entering cooldown for %s seconds", self._chat_id, wait)
                            handled = True
                    except Exception:
                        pass

                if handled:
                    continue

                if 'message is not modified' in msg:
                    logger.debug("Telegram edit ignored: message not modified")
                    continue
                logger.exception("Error updating Telegram message: %s", e)
