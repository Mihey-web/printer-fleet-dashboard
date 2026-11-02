# v21 2026-06-09
import dataclasses
import html
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional, Protocol

# .env in the project root (cp .env.example .env), read before any module below
# reads the environment at import. Keys already set by systemd / Docker / the
# shell win. Only when started as the program: importing main (tests) reads nothing.
if __name__ == '__main__':
    from app.services import env_file
    _ENV_FILE_KEYS = env_file.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
else:
    _ENV_FILE_KEYS = []

import uvicorn
from app.api.main import create_app
from app.collectors.bambu_collector import BambuCollector, quiet_vendor_logging, tls_verify_enabled
from app.collectors.creality_collector import CrealityCollector
from app.collectors.klipper_collector import KlipperCollector
from app.collectors.mks_wifi_collector import MksWifiCollector
from app.domain.models import MAINTENANCE_STATE, PrinterKind, PrinterState, PrinterStatus, now_ts

from app.services.normalizer import normalize_bambu, normalize_ws_dict
from app.services.state_store import StateStore, snapshot_reason
from app.services import ams_store as ams_store_module
from app.services.ams_store import AmsStore
from app.services import ams_autodry_engine, job_journal, notification_tracker, printer_commands, settings_service, state_store
from app.services import i18n as bot_i18n
from app.services.proxy_checker import RedactingFilter
import config

# The vendored pybambu (logger app.vendor.pybambu) and paho: ERROR only, rate-limited.
quiet_vendor_logging()
# Proxy URLs carry login:password: our own root handler masks them in every record
# and traceback. basicConfig is a no-op when root already has handlers (pytest),
# so foreign handlers are never touched.
_log_handler = logging.StreamHandler()
_log_handler.addFilter(RedactingFilter())
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    handlers=[_log_handler])
logger = logging.getLogger(__name__)


def _redact_uvicorn_logs() -> None:
    """Give uvicorn's own handlers the same masking (call after uvicorn.Config).

    uvicorn's error handler does not propagate to root (it prints ASGI
    tracebacks itself). uvicorn.access is left alone: its formatter needs
    record.args, and it logs no credentials.
    """
    for h in logging.getLogger('uvicorn').handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())

POLL_INTERVAL = max(getattr(config, 'POLL_INTERVAL', 60), 30)
MAX_WORKERS = getattr(config, 'MAX_WORKERS', 10)
OFFLINE_GRACE_PERIOD = max(getattr(config, 'OFFLINE_GRACE_PERIOD_SECONDS', 180), 0)
WEB_HOST = os.environ.get('WEB_HOST', getattr(config, 'WEB_HOST', '127.0.0.1'))
WEB_PORT = int(os.environ.get('WEB_PORT', getattr(config, 'WEB_PORT', 8000)))

# Telegram and proxy configuration lives in the settings table
# (app/services/settings_service) and is edited from the settings page.
# poll_loop reads it per event so edits apply without a restart.


class Notifier(Protocol):
    def notify(self, text: str) -> bool:
        ...


def build_collectors():
    # Fleet configuration lives in the printers table (app/services/printer_registry)
    # and is edited from the admin panel. The DB is the single source of truth —
    # a registry failure is fatal by design (no config.py fallback).
    from app.services import printer_registry
    rows = printer_registry.load_for_startup(config)
    items = []
    for r in rows:
        pid, label, host = r['id'], r['label'], r['host']
        if r['kind'] == 'bambu':
            device_type = r.get('model') or 'X1C'
            # Opt-in TLS certificate check per model (FORGE_BAMBU_TLS_VERIFY, see config.py).
            tls_verify = tls_verify_enabled(getattr(config, 'BAMBU_TLS_VERIFY', ''), device_type)
            items.append((pid, PrinterKind.BAMBU, label, BambuCollector(label, host, r['access_code'], r['serial'], device_type, tls_verify=tls_verify), device_type))
        elif r['kind'] == 'creality':
            items.append((pid, PrinterKind.CREALITY, label, CrealityCollector(label, host), r.get('model') or 'k1max'))
        elif r['kind'] == 'klipper':
            items.append((pid, PrinterKind.KLIPPER, label, KlipperCollector(label, host, int(r.get('port') or 7125)), r.get('model') or 'generic'))
        elif r['kind'] == 'mks':
            items.append((pid, PrinterKind.MKS, label, MksWifiCollector(label, host, int(r.get('port') or 8080)), r.get('model') or 'mks'))
    return items


def _note_signature(pid, status):
    """print.fun says whether the printer takes unsigned print commands
    (print_cmds without a probe). Never breaks the poll loop."""
    try:
        printer_commands.note_signature(pid, status.signature_required)
    except Exception:
        logger.debug('[%s] note_signature failed', pid, exc_info=True)


def _journal_observe(status, now):
    """Feed the print-job journal (app/services/job_journal) every polled status:
    live, grace clone or offline. Never breaks the poll loop."""
    try:
        job_journal.observe(status, now)
    except Exception:
        logger.debug('[%s] job journal observe failed', getattr(status, 'id', '?'))


def _close_bambu_collectors(collectors):
    """Stop the Bambu MQTT listeners (shutdown). One failing close() must not
    keep the others running."""
    for _pid, kind, label, collector, _device_type in collectors:
        if kind != PrinterKind.BAMBU:
            continue
        try:
            collector.close()
        except Exception:
            logger.debug('[%s] close failed', label, exc_info=True)


def _display_label(pid, fallback):
    """Актуальное имя принтера из реестра по стабильному id.

    status.label «запечён» в коллекторе на старте, поэтому переименование без
    рестарта не попадало в уведомления. Реестр — источник истины для имён;
    берём свежее имя оттуда, откатываясь на fallback при недоступности.
    """
    try:
        from app.services import printer_registry
        label = printer_registry.get_label(pid)  # short-TTL cached, not a raw SQLite read
        if label:
            return label
    except Exception:
        logger.debug("display label lookup failed for %s", pid, exc_info=True)
    return fallback


def _in_maintenance(pid):
    """True while an admin keeps the printer in maintenance mode
    (printer_registry, short-TTL cached); False when the registry cannot tell.
    Never breaks the poll loop."""
    try:
        from app.services import printer_registry
        return bool(printer_registry.in_maintenance(pid))
    except Exception:
        logger.debug("maintenance lookup failed for %s", pid, exc_info=True)
        return False


def _history_state(pid):
    """StateStore's history_state hook: a printer in maintenance mode writes
    'maintenance' rows (left out of the stats' shares and the event log)."""
    return MAINTENANCE_STATE if _in_maintenance(pid) else None


def offline_status(pid, label, kind, device_type=None, error=None):
    return PrinterStatus(
        id=pid,
        label=label,
        kind=kind,
        online=False,
        state=PrinterState.OFFLINE,
        last_update_ts=now_ts(),
        last_error=str(error) if error else None,
        device_type=device_type,
        grace_period_active=False,
        last_successful_fetch=0.0,
    )


def _effective_prev_state(prev_state, last_active_state):
    """Collapse a transient OFFLINE to the last active state we actually observed.

    A print that briefly drops offline (grace expired) between an active state and
    a terminal one (FINISHED/PAUSED/ERROR) would otherwise have prev_state==OFFLINE
    and never fire a notification. If we saw it active before, use that state.
    Returns prev_state unchanged when we never observed an active state — so a
    reconnect of an already-finished job stays quiet (last_active is None).
    """
    if prev_state == PrinterState.OFFLINE and last_active_state is not None:
        return last_active_state
    return prev_state


def _build_grace_status(prev_item, reason, now, device_type):
    """Clone the last good status for a printer inside its grace period.

    Uses dataclasses.replace so EVERY field is carried over — an earlier
    field-by-field copy silently dropped ams/fans/light_on/fw_update, blanking
    the AMS panel and fan/light indicators the moment a transient fetch failure
    put a printer into grace. Only the failure markers are overridden.
    """
    return dataclasses.replace(
        prev_item,
        last_update_ts=now,
        last_error=reason,
        device_type=device_type,
        grace_period_active=True,
    )


def _error_notification(template, label, last_error, status=None, lang=None):
    """Telegram error text. It goes out with parse_mode=HTML: the template's own
    markup stays, the label and the printer's error text are escaped (labels are
    free-form, and Bambu's HMS texts contain '>', e.g. «Настройки > Калибровка»;
    either would reject the whole message). The error is in the bot's language:
    Bambu's texts as the English original of their code (status.notifications)."""
    msg = template.replace('{label}', html.escape(str(label)))
    if last_error:
        msg += f': {html.escape(str(bot_i18n.printer_error(last_error, status, lang)))}'
    return msg


def _fmt_time(seconds, lang=None):
    # 5ч30м / 5h30m: the bot's language (app/services/i18n.py)
    return bot_i18n.duration(seconds, lang)

def _fmt_job(name):
    if not name:
        return None
    parts = [p.strip() for p in name.split('+') if p.strip()]
    if not parts:
        return None
    unique = []
    seen = set()
    for p in parts:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    if len(unique) == 1:
        count = len(parts)
        return unique[0] if count == 1 else f"{unique[0]} x{count}"
    return f"{unique[0]} +{len(parts)-1}" if len(parts) > 2 else " + ".join(parts)

def _state_sort_key(s):
    if _in_maintenance(s.id):
        return (6, s.label)            # in maintenance mode: last, whatever its live state
    if s.state == PrinterState.ERROR:
        group = 0
    elif s.state == PrinterState.PAUSED:
        group = 1
    elif s.state in (PrinterState.FINISHED, PrinterState.CANCELLED):
        group = 2
    elif s.state == PrinterState.PRINTING:
        group = 3
    elif s.state == PrinterState.IDLE:
        group = 4
    else:
        group = 5
    if group == 3:
        eta = s.eta_seconds if s.eta_seconds and s.eta_seconds > 0 else 86400
        return (group, eta, s.label)
    return (group, s.label)


def format_status_text(store: StateStore, lang: Optional[str] = None) -> str:
    """The bot's summary, in its language (settings «telegram_lang»): the texts are
    looked up in the panel's dictionary, names and jobs stay as they are."""
    lang = bot_i18n.bot_lang() if lang is None else lang
    _t = lambda text: bot_i18n.t(text, lang)
    printers = sorted(store.get_all(), key=_state_sort_key)

    lines = []
    for s in printers:
        # Use the current registry label (cached), not the one baked into the
        # collector at startup — otherwise a rename doesn't show in the bot status
        # until a restart. Dynamic values (label/job/error) go into a
        # parse_mode=HTML message, so a literal '<'/'&' would 400 the whole send;
        # escape them, the <b> markup is ours.
        label = html.escape(str(_display_label(s.id, s.label)))
        if _in_maintenance(s.id):
            lines.append(f"\U0001F527 <b>{label}</b> — {_t('на обслуживании')}")
        elif s.state == PrinterState.PRINTING or s.state == PrinterState.PAUSED:
            icon = "\U0001F7E2" if s.state == PrinterState.PRINTING else "\U0001F7E1"
            pct = f"{s.progress_pct:.0f}%" if s.progress_pct is not None else ""
            job = _fmt_job(s.job_name)
            if job:
                job = html.escape(job)
            line1 = f"{icon} <b>{label}</b> — {pct}"
            if job:
                line1 += f" · {job}"
            details = []
            if s.eta_seconds and s.eta_seconds > 0:
                details.append(f"\u23f1{_fmt_time(s.eta_seconds, lang)}")
            if s.print_time_seconds and s.print_time_seconds > 0:
                details.append(f"\u23f3{_fmt_time(s.print_time_seconds, lang)}")
            if s.nozzle_temp is not None and s.nozzle_temp > 0:
                t = f"\U0001F321{s.nozzle_temp:.0f}"
                if s.bed_temp is not None and s.bed_temp > 0:
                    t += f"/{s.bed_temp:.0f}°C"
                else:
                    t += "°C"
                details.append(t)
            if details:
                lines.append(line1 + "\n" + " · ".join(details))
            else:
                lines.append(line1)

        elif not s.online or s.state == PrinterState.OFFLINE:
            lines.append(f"\u2B1B {label} — {_t('офлайн')}")

        elif s.state == PrinterState.FINISHED:
            line = f"\u2705 <b>{label}</b> — {_t('завершено')}"
            job = _fmt_job(s.job_name)
            if job:
                line += f" · {html.escape(job)}"
            lines.append(line)

        elif s.state == PrinterState.ERROR:
            line = f"\U0001F534 <b>{label}</b> — {_t('ошибка')}"
            if s.last_error:
                line += f": {html.escape(str(bot_i18n.printer_error(s.last_error, s, lang)))}"
            lines.append(line)

        elif s.state == PrinterState.CANCELLED:
            # Not an error: last_error would show the cancel echo as a failure.
            lines.append(f"\u26AA {label} — {_t('отменено')}")

        else:
            line = f"\u26AA {label}"
            if s.nozzle_temp is not None and s.nozzle_temp > 0:
                line += f" · \U0001F321{s.nozzle_temp:.0f}"
                if s.bed_temp is not None and s.bed_temp > 0:
                    line += f"/{s.bed_temp:.0f}°C"
                else:
                    line += "°C"
            lines.append(line)

    return "\n".join(lines) if lines else _t("Нет данных о принтерах")





def poll_loop(
    store: StateStore,
    collectors,
    tg: Optional[Notifier] = None,
    prev_states: Optional[dict[str, PrinterState]] = None,
    fail_start: Optional[dict[str, float]] = None,
    ams_store: Optional[AmsStore] = None,
):
    _prev = prev_states or {}
    _fail_start = fail_start or {}
    _last_snapshot: dict[str, float] = {}
    # pid -> reason of the last history row: a new reason (another pause code)
    # is written at once, like a state change, not with the next minute's row.
    _last_reason: dict[str, Optional[str]] = {}
    # pid -> время последнего «финишного» уведомления (первого или повторного).
    # Пока принтер стоит FINISHED и включён повтор, шлём напоминание каждые
    # telegram_finish_repeat_interval_min минут; запись гасится при любом
    # уходе из FINISHED.
    _finish_notify_at: dict[str, float] = {}
    # pid -> последнее НЕ-offline состояние, которое мы реально наблюдали. Нужно,
    # чтобы кратковременный уход в OFFLINE (истёкший grace) между активной печатью
    # и терминальным состоянием не съедал уведомление: если после offline принтер
    # вернулся FINISHED/PAUSED, а до offline он ПЕЧАТАЛ — уведомление всё равно
    # уходит. На старте записи нет → реконнект уже-завершённой задачи не шумит.
    _last_active_state: dict[str, PrinterState] = {}
    # Size the pool to cover every printer so a few slow/offline devices can't
    # starve the rest of the fleet within a single cycle.
    workers = max(MAX_WORKERS, len(collectors)) or 1
    while True:
        cycle_start = time.monotonic()
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(c.fetch): (pid, kind, label, device_type, c) for pid, kind, label, c, device_type in collectors}
                for fut in as_completed(futures):
                    pid, kind, label, device_type, collector = futures[fut]
                    prev_state = _prev.get(pid)
                    try:
                        result = fut.result()
                        if kind == PrinterKind.BAMBU:
                            status = normalize_bambu(pid, label, result, device_type=device_type)
                            _note_signature(pid, status)
                        else:
                            status = normalize_ws_dict(pid, label, kind, result if isinstance(result, dict) else {}, device_type=device_type)

                        if status.online and (prev_state == PrinterState.OFFLINE or prev_state is None):
                            logger.info('[%s] Printer back online (was: %s)', label, prev_state.value if prev_state else '–')

                        if pid in _fail_start:
                            logger.info('[%s] восстановление после %d сек в grace period', label, int(time.time() - _fail_start[pid]))
                            del _fail_start[pid]

                        # Notification settings are read per event so edits from
                        # the settings page apply without a restart (cached dict).
                        scfg = settings_service.get_all() if tg is not None else {}
                        disp_label = _display_label(pid, status.label) if tg is not None else status.label
                        # Treat a transient OFFLINE (grace expired) as the last
                        # active state we actually saw, so a PRINTING→offline→
                        # FINISHED/PAUSED/ERROR transition still notifies. Defaults
                        # to prev_state (incl. OFFLINE) when we never saw it active,
                        # which keeps a reconnect of an already-finished job quiet.
                        effective_prev = _effective_prev_state(prev_state, _last_active_state.get(pid))
                        # In maintenance mode nothing is sent and no finish repeat
                        # is due; _prev / _last_active_state still follow it, so
                        # after the mode only a transition from then on notifies.
                        quiet = tg is not None and _in_maintenance(pid)
                        if tg is not None and not quiet and scfg.get('telegram_notify_on_finish'):
                            now = time.time()
                            if status.state == PrinterState.FINISHED:
                                entered = effective_prev == PrinterState.PRINTING
                                last = _finish_notify_at.get(pid)
                                repeat_due = False
                                if not entered and scfg.get('telegram_notify_on_finish_repeat') and last is not None:
                                    interval = max(1, int(scfg.get('telegram_finish_repeat_interval_min') or 30)) * 60
                                    repeat_due = (now - last) >= interval
                                if entered or repeat_due:
                                    try:
                                        # parse_mode=HTML: the free-form label is escaped.
                                        msg = bot_i18n.template(scfg, 'telegram_finish_template').format(label=html.escape(str(disp_label)))
                                        # Only anchor the repeat timer if the message
                                        # was actually dispatched. Marking it on a
                                        # no-op (bot down / no chat) would suppress
                                        # the real notification once the bot recovers.
                                        if tg.notify(msg):
                                            _finish_notify_at[pid] = now
                                            logger.info('[%s] Telegram finish notification sent%s',
                                                        label, ' (повтор)' if repeat_due else '')
                                    except Exception as e:
                                        logger.warning('[%s] Telegram notify failed: %s', label, e)
                                elif last is None:
                                    # Принтер уже стоял завершённым, когда мы начали
                                    # опрос (перехода PRINTING→FINISHED не видели) —
                                    # заякорим отсчёт, чтобы повторы шли от now.
                                    _finish_notify_at[pid] = now
                        if status.state != PrinterState.FINISHED or quiet:
                            _finish_notify_at.pop(pid, None)
                        if tg is not None and not quiet and scfg.get('telegram_notify_on_paused'):
                            if effective_prev == PrinterState.PRINTING and status.state == PrinterState.PAUSED:
                                try:
                                    msg = bot_i18n.template(scfg, 'telegram_paused_template').replace('{label}', html.escape(str(disp_label)))
                                    tg.notify(msg)
                                    logger.info('[%s] Telegram paused notification sent', label)
                                except Exception as e:
                                    logger.warning('[%s] Telegram notify failed: %s', label, e)
                        if tg is not None and not quiet and scfg.get('telegram_notify_on_error'):
                            if effective_prev is not None and status.state == PrinterState.ERROR and effective_prev not in (PrinterState.ERROR, PrinterState.OFFLINE):
                                try:
                                    msg = _error_notification(bot_i18n.template(scfg, 'telegram_error_template'), disp_label,
                                                              status.last_error, status, bot_i18n.bot_lang(scfg))
                                    tg.notify(msg)
                                    logger.info('[%s] Telegram error notification sent', label)
                                except Exception as e:
                                    logger.warning('[%s] Telegram notify failed: %s', label, e)
                        _prev[pid] = status.state
                        if status.state != PrinterState.OFFLINE:
                            _last_active_state[pid] = status.state
                        store.upsert(status)
                        _journal_observe(status, time.time())
                        last_ts = _last_snapshot.get(pid, 0)
                        # A row a minute, and one at once on a state or reason
                        # change: a pause the auto-ignore clears within the
                        # minute still reaches the event log, with its reason.
                        # Not in maintenance mode: its rows are all 'maintenance'
                        # anyway, an extra one would only cost an SD-card write.
                        row_reason = snapshot_reason(status)
                        if (time.time() - last_ts >= 60
                                or ((status.state != prev_state or row_reason != _last_reason.get(pid))
                                    and not _in_maintenance(pid))):
                            store.record_snapshot(status)
                            if ams_store is not None:
                                ams_store.record_ams(status)
                            _last_snapshot[pid] = time.time()
                            _last_reason[pid] = row_reason
                    except Exception as e:
                        reason = str(e)
                        prev_item = store.get_one(pid)
                        now = time.time()

                        # Determine if we should enter/continue grace period
                        if prev_item is None:
                            # No previous state — immediately offline
                            offline = offline_status(pid, label, kind, device_type=device_type, error=reason)
                            _prev[pid] = PrinterState.OFFLINE
                            store.upsert(offline)
                            _journal_observe(offline, now)
                            store.record_snapshot(offline)
                            continue

                        if pid not in _fail_start:
                            _fail_start[pid] = now
                            logger.warning('[%s] fetch failed — entering grace period (%ds): %s', label, OFFLINE_GRACE_PERIOD, reason)

                        elapsed = now - _fail_start[pid]

                        if elapsed < OFFLINE_GRACE_PERIOD:
                            # Grace period active: keep last good state with flag.
                            # dataclasses.replace copies every field (incl. ams/fans/
                            # light_on/fw_update the old manual copy dropped).
                            grace_status = _build_grace_status(prev_item, reason, now, device_type)
                            _prev[pid] = grace_status.state
                            store.upsert(grace_status)
                            _journal_observe(grace_status, now)
                            last_ts = _last_snapshot.get(pid, 0)
                            if now - last_ts >= 60:
                                store.record_snapshot(grace_status)
                                _last_snapshot[pid] = now
                        else:
                            # Grace expired — real offline
                            prev_was_offline = not prev_item.online
                            if not prev_was_offline:
                                logger.warning('[%s] OFFLINE — grace period expired (%ds): %s', label, int(elapsed), reason)
                            offline = offline_status(pid, label, kind, device_type=device_type, error=reason)
                            _prev[pid] = PrinterState.OFFLINE
                            store.upsert(offline)
                            _journal_observe(offline, now)
                            # Keep _fail_start set so a still-dead printer stays
                            # offline instead of re-entering a fresh grace period
                            # every cycle (which flapped the card offline↔stale and
                            # spammed the log). It's cleared on the first success.
                            last_ts = _last_snapshot.get(pid, 0)
                            if now - last_ts >= 60:
                                store.record_snapshot(offline)
                                _last_snapshot[pid] = now
        except RuntimeError:
            break
        except Exception:
            # Outermost safety net for the whole poll cycle. Never let it stay
            # silent — a swallowed error here means the dashboard shows stale
            # data with zero indication that the polling engine hiccupped.
            logger.error('poll_loop cycle failed unexpectedly', exc_info=True)
        # Deadline-based sleep: keep the effective cadence at POLL_INTERVAL
        # regardless of how long the fetch cycle took, instead of always adding
        # the full interval on top of the work time.
        elapsed = time.monotonic() - cycle_start
        time.sleep(max(0.0, POLL_INTERVAL - elapsed))


_LOOPBACK_HOSTS = ('localhost', '::1')


def cookie_secure_http_warning(host: str, port, cookie_secure: bool) -> Optional[str]:
    """One log line when sign-in will likely fail silently: Secure cookies, and
    the server reachable from other machines (opened there as plain http://).
    Behind an HTTPS proxy on the same host WEB_HOST is 127.0.0.1: no warning."""
    host = (host or '').strip().strip('[]')
    if not cookie_secure or host in _LOOPBACK_HOSTS or host.startswith('127.'):
        return None
    return ('COOKIE_SECURE=1 while the panel listens on %s:%s: browsers keep the login cookie '
            'only over HTTPS, so opened as http://<address>:%s the sign-in will not stick. '
            'Use an HTTPS proxy, or set COOKIE_SECURE=0 on a trusted LAN.' % (host, port, port))


def db_paths_that_are_directories() -> list:
    """Database paths that exist as directories. Docker creates one when a
    bind-mounted file (docker-compose.yml volumes) is missing on the host."""
    from app.api import auth
    return [p for p in (state_store.DB_PATH, auth.USERS_DB, ams_store_module.DB_PATH) if os.path.isdir(p)]


def main():
    if _ENV_FILE_KEYS:
        logger.info('.env: set %s', ', '.join(_ENV_FILE_KEYS))
    bad_db = db_paths_that_are_directories()
    if bad_db:
        # A clear line in `docker compose logs` instead of SQLite's "unable to open database file".
        logger.error('%s: a directory, not a database file. Docker creates it when the file is missing '
                     'on the host: stop the container, remove the empty directory, run `touch <file>`, '
                     'start again.', ', '.join(bad_db))
        raise SystemExit(1)
    # A printer in maintenance mode writes 'maintenance' history rows.
    store = StateStore(history_state=_history_state)
    # Notification first-seen times and the cancel flag, kept in the history DB
    # across restarts; loaded before anything upserts a status.
    notification_tracker.init(state_store.DB_PATH)
    ams_store = AmsStore()
    collectors = build_collectors()

    for pid, kind, label, c, device_type in collectors:
        store.register_collector(pid, c)

    # Settings table must exist before the admin API can serve its first
    # request (one-time migration from config.py happens here too).
    settings_service.load(config)

    # Команды с карточек идут через printer_commands (локальный MQTT клиента
    # коллектора).
    printer_commands.set_store(store)
    printer_commands.start_probe_loop(store)
    printer_commands.start_live_refresh()   # cards follow the live telemetry every 2 s, not every poll
    from app.services import auto_ignore
    auto_ignore.start(store)   # «Ignore and resume» for the print errors listed in settings
    from app.services import hms_catalog
    hms_catalog.start()        # Bambu's HMS button/text tables: data/hms, refreshed daily

    # AMS auto-drying: a daemon thread over the stored status and the AMS
    # history; drying starts go through printer_commands. Ships in shadow mode
    # (journal only) until an admin turns it off on the AMS tab.
    ams_autodry_engine.set_sources(store, ams_store)
    ams_autodry_engine.start()

    # Bind the HTTP port FIRST, in a background thread, before connecting to any
    # printer or probing the Telegram proxy. Those can take 60-90s when devices
    # are offline, and doing them before the bind left nginx returning transient
    # 502s after every restart. uvicorn skips signal-handler install when not on
    # the main thread, so running the server in a daemon thread is supported.
    app = create_app(store)
    server = uvicorn.Server(uvicorn.Config(app, host=WEB_HOST, port=WEB_PORT, log_level='info'))
    _redact_uvicorn_logs()  # after Config: it creates uvicorn's handlers
    threading.Thread(target=server.run, daemon=True).start()
    logger.info('Web dashboard: http://%s:%s', WEB_HOST, WEB_PORT)
    cookie_warning = cookie_secure_http_warning(WEB_HOST, WEB_PORT, getattr(config, 'COOKIE_SECURE', True))
    if cookie_warning:
        logger.warning(cookie_warning)

    # Telegram bot + proxy checker: settings live in the DB; telegram_manager
    # hot-applies edits from the settings page, so no restart is needed for them.
    from app.services import telegram_manager
    telegram_manager.init(lambda: format_status_text(store))
    tg = telegram_manager

    # Warm up Bambu MQTT sessions in the background. Offline printers no longer
    # block the (already-bound) HTTP port; the poll loop reconnects lazily anyway.
    def _connect_bambu():
        for pid, kind, label, c, device_type in collectors:
            if kind == PrinterKind.BAMBU:
                try:
                    c.connect()   # non-blocking: the MQTT listener connects in the background
                    logger.info('[%s] MQTT connect started', label)
                except Exception as e:
                    logger.warning('[%s] connect failed: %s', label, e)
    threading.Thread(target=_connect_bambu, daemon=True).start()

    # Print-job journal: continue the prints left open before the restart (on
    # the first start: rebuild past prints from the history in the background);
    # the poll loop feeds it every status (History → «Печати»).
    job_journal.start(store)

    # Run the poll loop on the main thread — it blocks forever and keeps the
    # process alive while the server runs in its daemon thread.
    try:
        poll_loop(store, collectors, tg, fail_start={}, ams_store=ams_store)
    finally:
        # Ctrl+C / SystemExit: stop the MQTT listeners and watchdogs cleanly.
        # (Under systemd SIGTERM ends the process at once; every lib thread is a daemon.)
        _close_bambu_collectors(collectors)


if __name__ == '__main__':
    main()
# ------------------------------------------------------
