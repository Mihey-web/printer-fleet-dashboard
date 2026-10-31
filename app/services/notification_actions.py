"""What a printer notification's buttons do: the action registry (notifications spec §5).

Bambu Studio draws a print-error dialog's buttons from Bambu's action table
(app/services/hms_catalog): every code lists button ids, each id is a handler
that sends one MQTT command. ACTIONS maps our own action keys to those ids and
holds everything a later run() needs: the payload builder, the report fields
it needs, the live checks, the confirmation and the expected outcome.
actions_for() turns a code into the buttons the dashboard shows.

klipper_actions() does the same for a Klipper host state.

run() presses one button: it re-checks everything on the printer's live
telemetry (fail closed), sends through printer_commands' channels and answers
with their verdicts. reset_skip_reason() / reset_one() are the fleet's
«Сбросить ошибки» (fleet_commands, action reset_errors). The registry part
does no I/O: the normalizer calls actions_for().
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.collectors.bambu_raw import CANCEL_CODES
from app.domain.models import PrinterState
from app.services import auto_ignore, hms_catalog, settings_service

logger = logging.getLogger(__name__)

# `uiop close` (system class, err in hex) is what Studio sends whenever its
# dialog closes. Whether it clears the error on the printer is not verified:
# off until a live check with the owner (actions_protocol.md §7).
ENABLE_UIOP = False

# Bambu button ids that are flags or links, not buttons.
ID_WIKI = 6          # «Подробнее»: drawn whenever the record has a wiki link
ID_BLOCKING = 39     # no close button: the user must pick one of the buttons
# The cancel echo has no record in Bambu's table: it gets «Закрыть диалог».
CANCEL_ACTIONS = ("clean",)
CANCEL_LABEL = "Закрыть диалог"

SRC_BAMBU = ("print_error", "hms")


def _dec(ctx: Dict[str, Any]) -> str:
    """print_error as Studio sends it in the hms commands: a decimal string."""
    return str(int(ctx["err"]))


def _resume(ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {"print": {"sequence_id": "0", "command": "resume", "err": _dec(ctx), "param": "reserve",
                      "job_id": str(ctx["job_id"])}}


def _ignore(ctx: Dict[str, Any]) -> Dict[str, Any]:
    # Imported here: printer_commands imports the normalizer, which imports this module.
    from app.services.printer_commands import build_error_ignore
    return build_error_ignore(int(ctx["err"]), ctx["job_id"])


def _stop(ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {"print": {"sequence_id": "0", "command": "stop", "err": _dec(ctx), "param": "reserve",
                      "job_id": str(ctx["job_id"])}}


def _ams_control(param: str) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def build(ctx: Dict[str, Any]) -> Dict[str, Any]:
        return {"print": {"sequence_id": "0", "command": "ams_control", "param": param}}
    return build


def _clean(ctx: Dict[str, Any]) -> Dict[str, Any]:
    # subtask_id goes out as the report has it, "" included (Studio does the same).
    return {"print": {"sequence_id": "0", "command": "clean_print_error", "subtask_id": str(ctx["subtask_id"]),
                      "print_error": int(ctx["err"])}}


def _idle_ignore(ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {"print": {"sequence_id": "0", "command": "idle_ignore", "err": _dec(ctx), "type": 0}}


def _simple(command: str, **extra: Any) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def build(ctx: Dict[str, Any]) -> Dict[str, Any]:
        return {"print": {"sequence_id": "0", "command": command, **extra}}
    return build


def _uiop_close(ctx: Dict[str, Any]) -> Dict[str, Any]:
    # The one command where err is hex ("%08X"), not decimal.
    return {"system": {"sequence_id": "0", "command": "uiop", "name": "print_error", "action": "close",
                       "source": 1, "type": "dialog", "err": "%08X" % (int(ctx["err"]) & 0xFFFFFFFF)}}


@dataclass(frozen=True)
class Spec:
    """One action. guards (checked live by run()): "pe" = print_error still
    equals the code; "pause" = gcode_state PAUSE; "job_attr" = not a laser /
    cutter job (Studio's check_resume_condition); "live" = the code is still
    reported (print_error or hms[]); "host_down" / "host_error" = Klipper's
    host state is shutdown or error / error. needs: report fields the payload
    takes (err = the code, job_id, subtask_id). expect: what the fast refresh
    waits for after a send."""
    label: str
    ids: Tuple[int, ...] = ()
    builder: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    rpc: Optional[str] = None                 # Moonraker method (Klipper actions)
    needs: Tuple[str, ...] = ()
    guards: Tuple[str, ...] = ()
    confirm: Optional[str] = None
    yes: Optional[str] = None
    danger: bool = False
    expect: Optional[str] = None              # "printing" | "stopped" | "cleared" | "ctl_ready"
    label_by_id: Dict[int, str] = field(default_factory=dict)
    kinds: Tuple[str, ...] = ("bambu",)
    srcs: Tuple[str, ...] = ("print_error",)
    enabled: bool = True

    def label_for(self, bambu_id: Optional[int]) -> str:
        return self.label_by_id.get(bambu_id, self.label) if bambu_id is not None else self.label


ACTIONS: Dict[str, Spec] = {
    "resume": Spec(
        "Продолжить печать", ids=(2, 3, 4, 12, 28), builder=_resume, needs=("err", "job_id"),
        guards=("pe", "pause", "job_attr"), expect="printing",
        label_by_id={2: "Продолжить печать", 3: "Продолжить (дефекты допустимы)", 4: "Продолжить (заправил)",
                     12: "Пруток заправлен — продолжить", 28: "Устранено — продолжить"}),
    "ignore": Spec(
        "Игнорировать и продолжить", ids=(27, 25), builder=_ignore, needs=("err", "job_id"),
        guards=("pe", "pause", "job_attr"), expect="printing"),
    # Studio sends stop without the job_attr check.
    "stop": Spec(
        "Остановить печать", ids=(5,), builder=_stop, needs=("err", "job_id"), guards=("pe",),
        confirm="Остановить печать? Задание будет потеряно.", yes="Стоп", danger=True, expect="stopped"),
    "ams_done": Spec(
        "Готово", ids=(7, 10001), builder=_ams_control("done"), guards=("pe",),
        label_by_id={7: "Пруток вышел — продолжить", 10001: "Готово"}),
    "ams_retry": Spec(
        "Повторить", ids=(8, 9, 34, 10002), builder=_ams_control("resume"), guards=("pe", "job_attr"),
        expect="printing",
        label_by_id={8: "Не вышел — повторить", 9: "Готово — продолжить", 34: "Повторить (устранено)",
                     10002: "Повторить"}),
    "ams_abort": Spec(
        "Прервать операцию AMS", ids=(51,), builder=_ams_control("abort"), guards=("pe",),
        confirm="Прервать операцию AMS?", yes="Прервать", danger=True),
    # Any printer state: the cancel echo is cleared from a FAILED printer.
    "clean": Spec(
        CANCEL_LABEL, ids=(11, 10004), builder=_clean, needs=("subtask_id",), guards=("pe",),
        expect="cleared", label_by_id={11: "ОК", 10004: "ОК"}),
    "idle_ignore": Spec(
        "Не напоминать", ids=(23,), builder=_idle_ignore, needs=("err",), guards=("pe", "job_attr")),
    # The codes these answer stay listed while the condition lasts; both
    # sources: the tables put some of them on print errors, some on HMS codes.
    "buzzer_off": Spec(
        "Выключить сирену", ids=(29,), builder=_simple("buzzer_ctrl", mode=0), guards=("live",), srcs=SRC_BAMBU),
    "dry_stop": Spec(
        "Остановить сушку", ids=(35,), builder=_simple("auto_stop_ams_dry"), guards=("live",), srcs=SRC_BAMBU),
    "refresh_nozzle": Spec(
        "Перепроверить хотэнд", ids=(24,), builder=_simple("refresh_nozzle"), guards=("live",), srcs=SRC_BAMBU),
    "purifier_off": Spec(
        "Выключить очистку воздуха", ids=(54,), builder=_simple("close_air_filt"), guards=("live",),
        srcs=SRC_BAMBU),
    # Klipper: Moonraker JSON-RPC, never through the general send() (not in KLIPPER_ACTIONS).
    "fw_restart": Spec(
        "Перезапуск прошивки", rpc="printer.firmware_restart", guards=("host_down",),
        confirm="Перезапустить прошивку? Klipper и плата перезагрузятся, печать будет потеряна.",
        yes="Перезапустить", danger=True, expect="ctl_ready", kinds=("klipper",), srcs=("klipper",)),
    "klippy_restart": Spec(
        "Перезапуск Klipper", rpc="printer.restart", guards=("host_error",), confirm="Перезапустить Klipper?",
        yes="Перезапустить", expect="ctl_ready", kinds=("klipper",), srcs=("klipper",)),
    # No Bambu id: Studio sends it on every close. Off, see ENABLE_UIOP.
    "close_dialog": Spec(
        CANCEL_LABEL, builder=_uiop_close, needs=("err",), guards=("pe",), enabled=ENABLE_UIOP),
}

# Bambu button id -> action key. Ids without a handler in Studio (wizards on the
# printer's screen or in Handy: 1, 10, 13-22, 32, 33, 36, 37, 40-48, 50, 52, 56,
# 58, 67, 10000) and the flags 6 / 39 have no entry: they are never buttons.
ACTION_BY_ID: Dict[int, str] = {i: key for key, spec in ACTIONS.items() for i in spec.ids}


def _code_int(code: Any) -> Optional[int]:
    try:
        return int(str(code).replace("_", ""), 16)
    except (TypeError, ValueError):
        return None


def pe_label(value: int) -> str:
    """0x05008062 -> "0500_8062", as the printer's screen and the settings spell it."""
    h = "%08X" % (value & 0xFFFFFFFF)
    return h[:4] + "_" + h[4:]


def _auto_codes() -> List[str]:
    """The auto-ignore codes, from the settings cache only (never the DB: this
    runs in the normalizer); their defaults before the cache is loaded."""
    return list(settings_service.peek("auto_ignore_print_errors") or [])


def describe(picked: Iterable[Tuple[str, Optional[int]]]) -> List[Dict[str, Any]]:
    """[(key, bambu_id)] -> the records' "actions" list. The first action that
    is not dangerous is the primary one; a dangerous one is never primary."""
    out, primary = [], False
    for key, bambu_id in picked:
        spec = ACTIONS[key]
        style = "danger" if spec.danger else ("" if primary else "primary")
        primary = primary or style == "primary"
        out.append({"key": key, "label": spec.label_for(bambu_id), "style": style,
                    "confirm": spec.confirm, "yes": spec.yes, "bambu_id": bambu_id})
    return out


def table_known(prefix: Optional[str], code: Any) -> bool:
    """Whether Bambu's action table decided this code's buttons (False: the
    table of this model is not loaded, and actions_for used its fallback)."""
    return hms_catalog.action_ids(prefix, code)[1]


# Klipper host state -> its notification's buttons, in this order.
KLIPPER_BY_HOST: Dict[str, Tuple[str, ...]] = {"shutdown": ("fw_restart",),
                                               "error": ("klippy_restart", "fw_restart")}


def klipper_actions(host_state: Any) -> List[Dict[str, Any]]:
    """A Klipper host-state notification's buttons (Moonraker restarts)."""
    keys = KLIPPER_BY_HOST.get(str(host_state or "").lower(), ())
    return describe((key, None) for key in keys if ACTIONS[key].enabled)


def actions_for(src: str, code: Any, prefix: Optional[str], kind: str = "bambu") -> Tuple[List[Dict[str, Any]], bool]:
    """A Bambu notification's buttons -> (actions, blocking).

    The ids come from Bambu's table (the model's record, else "default", never
    merged; hms_catalog.action_ids), in the table's order, each id once as its
    action key; ids without a handler are dropped, and so are actions that do
    not apply to this source (a print-error command on an HMS code would always
    be refused). blocking: the table has id 39. The cancel echo gets
    «Закрыть диалог». Without the model's table: «Игнорировать и продолжить»
    for the auto-ignore codes, nothing for the rest.
    """
    if kind != "bambu" or src not in SRC_BAMBU:
        return [], False
    value = _code_int(code)
    if value is None:
        return [], False
    if src == "print_error" and value in CANCEL_CODES:
        return describe((key, None) for key in CANCEL_ACTIONS), False
    ids, from_table = hms_catalog.action_ids(prefix, code)
    if not from_table:
        ids = [ACTIONS["ignore"].ids[0]] if src == "print_error" and pe_label(value) in _auto_codes() else []
    picked: List[Tuple[str, Optional[int]]] = []
    for bambu_id in ids:
        key = ACTION_BY_ID.get(bambu_id)
        spec = ACTIONS.get(key) if key else None
        if spec is None or not spec.enabled or kind not in spec.kinds or src not in spec.srcs:
            continue
        if key not in (k for k, _ in picked):
            picked.append((key, bambu_id))
    return describe(picked), ID_BLOCKING in ids


# --- running an action (spec §5.1) ---------------------------------------------------
#
# Fail closed: nothing is sent while anything is in doubt. The live status is
# normalized on the spot from the collector's client (never the poll loop's
# stored status, up to POLL_INTERVAL old), the record must still be there with
# this button, the action's guards must hold, a dangerous one must be confirmed,
# and one action per printer is on its way at a time.

MSG_UNKNOWN_ACTION = "Неизвестное действие"
MSG_GONE = "Уведомление уже снято"
MSG_NOT_LISTED = "Это действие не положено для этого кода"
MSG_DIALOG_CLOSED = "Диалог уже закрыт"
MSG_KLIPPY_STATE = "Klipper уже не в этом состоянии — обновите карточку"
MSG_CONFIRM = "Нужно подтверждение"
MSG_INFLIGHT = "Ждём ответа на предыдущее действие"
# The dashboard's /command says the same for a claimed unit (app/api/admin.py).
MSG_AUTODRY_BUSY = "Автосушка сейчас отправляет команду этому блоку — повторите через несколько секунд"
DRY_STOP = "dry_stop"

# A second action on the same printer within this window is refused (the mark
# is claimed right before the send, check and set in one step, under
# printer_commands._inflight_lock; a failed send gives it back).
ACTION_GUARD_S = 10.0
# After a sent action the printer is re-read live at most this long (fast refresh).
REFRESH_S = 60.0
_marks: Dict[str, float] = {}      # printer_id -> monotonic of the last claim

# A Klipper record's key -> the host state it was built from (notifications.build_klipper).
KLIPPER_HOST_BY_KEY = {"klipper:shutdown": "shutdown", "klipper:error": "error"}
_PE_GUARDS = ("pe", "pause", "job_attr")


def _live(pc: Any, printer_id: str, prn: Dict[str, Any]) -> Optional[Tuple[Any, Dict[str, Any]]]:
    """(live status, print-error context) or None: Bambu from the collector's
    device (printer_commands._live_view), Klipper from its client's data."""
    if prn.get("kind") == "bambu":
        view = pc._live_view(printer_id, prn)
        return (view.status, pc._pe_context(view)) if view is not None else None
    status = pc.live_status(printer_id, prn)
    return (status, {}) if status is not None else None


def _record(status: Any, key: str) -> Optional[Dict[str, Any]]:
    return next((n for n in getattr(status, "notifications", None) or [] if n.get("key") == key), None)


def _guard_refusal(pc: Any, action: str, spec: Spec, n: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    """Why the action's guards forbid it on this live record, or None."""
    guards = tuple(g for g in spec.guards if g in _PE_GUARDS) + (("job_id",) if "job_id" in spec.needs else ())
    if guards:
        err = _code_int(n.get("code")) if n.get("src") == "print_error" else None
        if err is None:
            return MSG_NOT_LISTED                    # a print-error command on another source
        failed = pc.print_error_check(ctx, err, guards)
        if failed:
            return {"pe": MSG_DIALOG_CLOSED if action == "clean" else pc.MSG_NOT_PAUSED_ON,
                    "pause": pc.MSG_NOT_PAUSED, "job_attr": pc.MSG_LASER_JOB, "job_id": pc.MSG_NO_JOB_ID}[failed]
    host = KLIPPER_HOST_BY_KEY.get(n.get("key"))
    if "host_down" in spec.guards and host not in ("shutdown", "error"):
        return MSG_KLIPPY_STATE
    if "host_error" in spec.guards and host != "error":
        return MSG_KLIPPY_STATE
    return None


def _claim(pc: Any, printer_id: str) -> Optional[float]:
    with pc._inflight_lock:
        last = _marks.get(printer_id)
        if last is not None and pc._monotonic() - last < ACTION_GUARD_S:
            return None
        mark = pc._monotonic()
        _marks[printer_id] = mark
        return mark


def _release(pc: Any, printer_id: str, mark: float) -> None:
    with pc._inflight_lock:
        if _marks.get(printer_id) == mark:
            del _marks[printer_id]


def _expect(spec: Spec, key: str) -> Optional[Callable[[Any], bool]]:
    """What the fast refresh waits for after the action (None: only settling)."""
    if spec.expect == "printing":
        return lambda s: s.state == PrinterState.PRINTING
    if spec.expect == "stopped":
        return lambda s: s.state not in (PrinterState.PRINTING, PrinterState.PAUSED)
    if spec.expect == "cleared":
        return lambda s: _record(s, key) is None
    if spec.expect == "ctl_ready":
        return lambda s: s.ctl_ready is True
    return None


def _run_live(pc: Any, printer_id: str, prn: Dict[str, Any], key: str, action: str, spec: Spec,
              live: Tuple[Any, Dict[str, Any]], confirmed: bool, source: str) -> Dict[str, Any]:
    status, ctx = live
    n = _record(status, key)
    if n is None:
        return pc._refused(MSG_GONE)
    if action not in [a.get("key") for a in n.get("actions") or []]:
        return pc._refused(MSG_NOT_LISTED)
    refusal = _guard_refusal(pc, action, spec, n, ctx)
    if refusal:
        return pc._refused(refusal)
    if spec.confirm and confirmed is not True:
        return pc._refused(MSG_CONFIRM)
    mark = _claim(pc, printer_id)
    if mark is None:
        return pc._refused(MSG_INFLIGHT)
    # The printer-wide drying stop goes past the dashboard's /command: the
    # auto-dry engine must know it as the admin's stop of its units (a hold, no
    # restart of an "interrupted" run), and they stay claimed meanwhile.
    dry_units: List[int] = []
    claimed: List[int] = []
    r: Dict[str, Any] = {}
    try:
        if action == DRY_STOP:
            from app.services import ams_autodry_engine as autodry
            dry_units = autodry.drying_units(printer_id, status)
            for ams in dry_units:
                if not autodry.claim(printer_id, ams):
                    return pc._refused(MSG_AUTODRY_BUSY)
                claimed.append(ams)
        if spec.rpc:
            r = pc.send_klipper_rpc(printer_id, action)
        else:
            payload = spec.builder({"err": _code_int(n.get("code")) if n.get("src") == "print_error" else None,
                                    "job_id": ctx.get("job_id"), "subtask_id": ctx.get("subtask_id") or ""})
            r = pc._send_local(printer_id, payload)
            pc._note_reply(printer_id, payload, r)
        if dry_units and r.get("success"):
            for ams in dry_units:
                autodry.note_user_command(printer_id, "dry_stop", {"ams": ams})    # never raises
    finally:
        for ams in claimed:
            autodry.unclaim(printer_id, ams)
        if not r.get("success"):
            _release(pc, printer_id, mark)
    out = pc._outcome(r)
    logger.info("[notif] %s %s on %s (%s) -> %s %s", printer_id, action, n.get("code"), source, out["status"],
                out["detail"])
    if out["success"]:
        pc._start_fast_refresh(printer_id, prn, _expect(spec, key), REFRESH_S)
    return out


def run(printer_id: str, key: str, action: str, confirmed: bool = False, source: str = "user") -> Dict[str, Any]:
    """Press button `action` of notification `key` on the printer: {"success",
    "status", "detail", "replied"} as printer_commands.send() (status confirmed /
    sent / rejected, or refused when nothing was sent). source: who pressed it
    ("user", "bulk"), for the log."""
    from app.services import printer_commands as pc, printer_registry

    spec = ACTIONS.get(action)
    if spec is None or not spec.enabled:
        return pc._refused(MSG_UNKNOWN_ACTION)
    prn = printer_registry.get_printer(printer_id)
    if prn is None:
        return pc._refused(pc.MSG_KIND)
    if prn.get("kind") not in spec.kinds:
        return pc._refused(pc.MSG_NOT_SUPPORTED)
    live = _live(pc, printer_id, prn)
    if live is None:
        return pc._refused(pc.MSG_NO_TELEMETRY)
    return _run_live(pc, printer_id, prn, key, action, spec, live, confirmed, source)


# --- the fleet's «Сбросить ошибки» (fleet_commands, reset_errors; spec §5.3) ------------
#
# Only «Закрыть диалог» on a print-error record that has it and decides nothing:
# never a pause (the buttons in its notification decide) and never one Bambu
# marks as a required choice (id 39). Mirrored by the dashboard's nfResetSkipWhy.

RESET_EMPTY = "уведомлений нет"
RESET_HMS_ONLY = "только HMS — сбрасывать нечего, принтер снимет их сам"
RESET_HMS_FATAL = "HMS не сбрасываются командой — принтер снимет сам, когда причину устранят"
RESET_PE = "ошибка %s — команды сброса нет, решите на экране принтера"
RESET_PAUSE = "пауза-ошибка %s — решается кнопками в уведомлении"
RESET_BLOCKING = "%s — выбор обязателен: решите кнопками в уведомлении"
RESET_BLOCKING_TEXT = {"0300_8007": "0300_8007 после отключения питания — выбор обязателен: продолжить или остановить"}
RESET_AUTO_TAIL = ", автопропуск уже пробует"
RESET_KLIPPER = "Klipper: перезапуск прошивки — только по одному, с подтверждением"
RESET_KLIPPER_NONE = "Klipper: команды сброса нет — устраните причину на принтере"
RESET_CREALITY = "Creality: команды сброса нет — только на экране принтера"
RESET_GONE = "сбрасывать нечего — уведомление уже снято"
RESET_CHANGED = "состояние изменилось, не отправлено"      # the dashboard's own words (nfResetRevalidate)
RESET_ACTION = "clean"


def resettable(items: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The record the fleet reset closes, or None."""
    for n in items or []:
        if (n.get("src") == "print_error" and not n.get("blocking") and n.get("severity") != "pause"
                and RESET_ACTION in [a.get("key") for a in n.get("actions") or []]):
            return n
    return None


def auto_state(printer_id: str, n: Dict[str, Any], codes: Optional[Iterable[str]] = None) -> Optional[Dict[str, Any]]:
    """The auto-ignore thread's state for a print-error record whose code is in
    the setting (auto_ignore.view), else None. codes: the setting's list, when
    the caller read it already (default: the settings cache)."""
    code = n.get("code")
    if n.get("src") != "print_error" or code not in (codes if codes is not None else _auto_codes()):
        return None
    value = _code_int(code)
    return auto_ignore.view(printer_id, value) if value is not None else None


def _held_skip(printer_id: str, n: Dict[str, Any]) -> str:
    code = str(n.get("code") or "")
    if n.get("blocking"):
        text = RESET_BLOCKING_TEXT.get(code) or RESET_BLOCKING % code
    else:
        text = RESET_PAUSE % code
    auto = n.get("auto") or auto_state(printer_id, n)
    if auto and auto.get("skip") is None and (auto.get("tries") or 0) < (auto.get("max") or 0):
        text += RESET_AUTO_TAIL
    return text


def reset_skip_reason(d: Dict[str, Any], caps: Optional[Dict[str, Any]] = None,
                      note: Optional[str] = None) -> Optional[str]:
    """Why the fleet reset leaves this printer (a PrinterStatus.to_dict() row)
    out (ru, lower case), or None when it closes one of its records. caps /
    note: printer_commands.caps_for(d), when the caller has them."""
    from app.services import fleet_commands, printer_commands as pc

    if not d.get("online") or d.get("state") == "offline":
        return fleet_commands.MSG_OFFLINE
    if d.get("grace_period_active"):
        return fleet_commands.MSG_GRACE
    items = d.get("notifications") or []
    kind = d.get("kind")
    if kind == "klipper" and items:
        return RESET_KLIPPER if any(n.get("actions") for n in items) else RESET_KLIPPER_NONE
    if kind == "creality" and items:
        return RESET_CREALITY
    if caps is None:
        caps, note = pc.caps_for(d)
    if caps.get("resume") is not True:
        return note or pc.NOTE_NOT_WIRED
    if not items:
        return RESET_EMPTY
    if resettable(items) is not None:
        return None
    for n in items:
        if n.get("blocking") or n.get("severity") == "pause":
            return _held_skip(str(d.get("id")), n)
    pe = next((n for n in items if n.get("src") == "print_error"), None)
    if pe is not None:
        return RESET_PE % pe.get("code")
    if any(n.get("severity") == "fatal" for n in items):
        return RESET_HMS_FATAL
    return RESET_HMS_ONLY


def reset_one(printer_id: str, action: str = "reset_errors", params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """fleet_commands.run_bulk's send for reset_errors: run() «Закрыть диалог»
    on the record the printer's live status still has to close. params["key"]:
    the record the admin confirmed in the dialog — another one live is refused."""
    from app.services import printer_commands as pc, printer_registry

    prn = printer_registry.get_printer(printer_id)
    if prn is None or prn.get("kind") != "bambu":
        return pc._refused(RESET_GONE)
    live = _live(pc, printer_id, prn)
    if live is None:
        return pc._refused(pc.MSG_NO_TELEMETRY)
    n = resettable(getattr(live[0], "notifications", None) or [])
    if n is None:
        return pc._refused(RESET_GONE)
    want = (params or {}).get("key")
    if want is not None and want != n.get("key"):
        return pc._refused(RESET_CHANGED)
    return _run_live(pc, printer_id, prn, n["key"], RESET_ACTION, ACTIONS[RESET_ACTION], live, False, "bulk")
