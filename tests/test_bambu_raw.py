"""RawReport: raw report fields the vendored pybambu drops or keeps private.

The expected values are parsed from the sanitized fixtures independently of
the code under test (last value of each key wins, as with the P1S deltas).
"""
import pytest

import bambu_testkit as kit
from app.collectors.bambu_raw import RawReport

MODELS = ("X1C", "P2S", "H2S", "P1S")


def _expected(model: str) -> dict:
    fun = fun2 = ams_status = tray_tar = ext_state = None
    home_flag_seen = False
    units, extruders = {}, {}
    for line in kit.load_fixture(model):
        p = line["msg"]["print"]
        if "fun" in p:
            fun = int(p["fun"], 16)
        if "fun2" in p:
            fun2 = int(p["fun2"], 16)
        if "home_flag" in p:
            home_flag_seen = True
        if "ams_status" in p:
            ams_status = int(p["ams_status"])
        ams = p.get("ams") or {}
        if "tray_tar" in ams:
            tray_tar = int(ams["tray_tar"])
        for u in ams.get("ams", []):
            cur = units.setdefault(int(u["id"]), {"info": None, "sf": None})
            if "info" in u:
                cur["info"] = int(u["info"], 16)
            if "dry_sf_reason" in u:
                cur["sf"] = tuple(u["dry_sf_reason"])
        ext = (p.get("device") or {}).get("extruder") or {}
        if "state" in ext:
            ext_state = ext["state"]
        for e in ext.get("info", []):
            cur = extruders.setdefault(e["id"], {})
            cur.update({k: e[k] for k in ("stat", "snow", "star", "info") if k in e})
    return {"fun": fun, "fun_seen": fun is not None, "fun2": fun2, "home_flag_seen": home_flag_seen,
            "ams_status": ams_status, "tray_tar": tray_tar, "ams": units,
            "ext_state": ext_state, "extruders": extruders}


@pytest.mark.parametrize("model", MODELS)
def test_raw_report_from_fixtures(model, offline_collector):
    collector = offline_collector(model)
    for line in kit.load_fixture(model):
        kit.deliver(collector, line["msg"])
    snap = collector.client.get_device().forge_raw.snapshot()
    want = _expected(model)
    assert {k: snap[k] for k in want} == want
    assert snap["busy"] is False          # every capture is a running print, no filament change


def test_raw_report_fixture_anchors(offline_collector):
    # Literal values (fixtures/bambu/README.md) as an anchor independent of _expected().
    snaps = {}
    for model in MODELS:
        collector = offline_collector(model)
        for line in kit.load_fixture(model):
            kit.deliver(collector, line["msg"])
        snaps[model] = collector.client.get_device().forge_raw.snapshot()

    p2s = snaps["P2S"]
    assert (p2s["fun"], p2s["fun2"]) == (0x60039FD193FF9CB7, 0x3973)
    assert p2s["ams"] == {0: {"info": 0x10002003, "sf": ()}}
    assert p2s["ext_state"] == 1
    assert p2s["extruders"] == {0: {"stat": 197376, "snow": 0, "star": 65535, "info": 1098}}
    assert (p2s["ams_status"], p2s["tray_tar"]) == (768, 0)

    h2s = snaps["H2S"]
    assert h2s["fun2"] == 0x1177
    assert h2s["ams"][0]["info"] == 0x10001003
    assert (h2s["extruders"][0]["snow"], h2s["extruders"][0]["star"]) == (1, 1)

    x1c = snaps["X1C"]
    assert x1c["fun2"] is None and x1c["fun_seen"] is True
    assert x1c["ams"] == {}
    assert x1c["extruders"][0]["snow"] == 65280

    p1s = snaps["P1S"]
    assert (p1s["fun"], p1s["fun_seen"], p1s["ext_state"]) == (None, False, None)
    assert p1s["tray_tar"] == 254
    assert p1s["home_flag_seen"] is True
    assert p1s["extruders"] == {}


def test_raw_report_merges_p1_deltas():
    raw = RawReport()
    raw.update({"command": "push_status", "msg": 0, "ams_status": 768, "home_flag": 1,
                "ams": {"tray_tar": "254", "ams": [{"id": "0", "info": "10002003", "dry_sf_reason": [6]}]}})
    raw.update({"command": "push_status", "msg": 1, "mc_percent": 5})            # a delta without those keys
    raw.update({"msg": 1, "ams": {"ams": [{"id": "0", "dry_sf_reason": []}]}})   # info missing: kept
    snap = raw.snapshot()
    assert snap["ams_status"] == 768 and snap["tray_tar"] == 254 and snap["home_flag_seen"] is True
    assert snap["ams"] == {0: {"info": 0x10002003, "sf": ()}}


def test_raw_report_ignores_garbage():
    raw = RawReport()
    raw.update({"fun": "20011830F9CFF", "fun2": "3973", "ams_status": 768,
                "ams": {"tray_tar": "1", "ams": [{"id": "0", "info": "10942023", "dry_sf_reason": [6]}]},
                "device": {"extruder": {"state": 1, "info": [{"id": 0, "stat": 3, "snow": 1, "star": 2, "info": 2}]}}})
    before = raw.snapshot()
    for junk in (
        None, [], "x", 5,
        {"fun": "zz", "fun2": None, "ams_status": "?", "home_flag": None},
        {"ams": {"tray_tar": "x", "ams": [1, 2, None, {"id": "x"}, {"id": "0", "info": "zz", "dry_sf_reason": "x"}]}},
        {"ams": {"ams": "nope"}}, {"ams": None}, {"ams": []},
        {"device": {"extruder": {"state": "x", "info": None}}},
        {"device": {"extruder": {"info": [None, 3, {"id": "?"}, {"id": 0, "snow": "x"}]}}},
        {"device": None}, {"device": {"extruder": []}},
    ):
        raw.update(junk)      # never raises
    assert raw.snapshot() == before


def test_snapshot_is_a_copy():
    raw = RawReport()
    raw.update({"ams": {"ams": [{"id": "0", "info": "3", "dry_sf_reason": [2]}]},
                "device": {"extruder": {"state": 1, "info": [{"id": 0, "info": 2}]}}})
    snap = raw.snapshot()
    snap["ams"][0]["info"] = 99
    snap["extruders"][0]["info"] = 99
    snap["ams"][5] = {}
    again = raw.snapshot()
    assert again["ams"] == {0: {"info": 3, "sf": (2,)}}
    assert again["extruders"] == {0: {"info": 2}}


# --- job_seq: job starts seen on the paho thread (spec_job_journal §5.2) ---

def test_job_seq_counts_starts_from_idle_finish_failed():
    import time

    raw = RawReport()
    assert (raw.snapshot()["job_seq"], raw.snapshot()["job_started_at"]) == (0, None)
    for state in ("RUNNING", "PAUSE", "RUNNING"):   # first state after the start; RUNNING <-> PAUSE
        raw.update({"gcode_state": state})
    assert (raw.snapshot()["job_seq"], raw.snapshot()["job_started_at"]) == (0, None)
    starts = []
    for before, start in (("FINISH", "RUNNING"), ("IDLE", "PREPARE"), ("FAILED", "slicing"), ("IDLE", "PAUSE")):
        raw.update({"gcode_state": before})
        raw.update({"command": "push_status", "msg": 1, "mc_percent": 3})   # a P1 delta: no gcode_state
        t = time.time()
        raw.update({"gcode_state": start})
        raw.update({"gcode_state": "RUNNING"})                                # PREPARE -> RUNNING: the same job
        snap = raw.snapshot()
        starts.append(snap["job_seq"])
        assert t <= snap["job_started_at"] <= time.time()
    assert starts == [1, 2, 3, 4]


def test_job_seq_skips_unknown_states_and_garbage():
    raw = RawReport()
    raw.update({"gcode_state": "IDLE"})
    raw.update({"gcode_state": "INIT"})        # neither idle nor working: not remembered
    raw.update({"gcode_state": "RUNNING"})
    assert raw.snapshot()["job_seq"] == 1
    raw.update({"gcode_state": "UNKNOWN"})     # a reconnect blip mid-print: no new job
    raw.update({"gcode_state": "RUNNING"})
    before = raw.snapshot()
    for junk in ({"gcode_state": None}, {"gcode_state": 5}, {"gcode_state": ""}, {"gcode_state": ["IDLE"]},
                 {"gcode_state": {"x": 1}}, {"gcode_state": "  "}):
        raw.update(junk)                       # never raises, changes nothing
    assert raw.snapshot() == before and before["job_seq"] == 1


@pytest.mark.parametrize("model", MODELS)
def test_job_seq_through_the_collector(model, offline_collector):
    # Every capture is a print already running when it began: no start seen.
    collector = offline_collector(model)
    for line in kit.load_fixture(model):
        kit.deliver(collector, line["msg"])
    raw = collector.client.get_device().forge_raw
    assert (raw.snapshot()["job_seq"], raw.snapshot()["job_started_at"]) == (0, None)
    kit.deliver(collector, {"print": {"command": "push_status", "msg": 0, "gcode_state": "FINISH"}})
    kit.deliver(collector, {"print": {"command": "push_status", "msg": 0, "gcode_state": "PREPARE"}})
    assert raw.snapshot()["job_seq"] == 1 and isinstance(raw.snapshot()["job_started_at"], float)


def test_busy_transitions_are_counted():
    # new protocol: device.extruder.state bit 19 = filament change in progress
    raw = RawReport()
    raw.update({"device": {"extruder": {"state": 1, "info": [{"id": 0, "stat": 0}]}}})
    assert (raw.snapshot()["busy"], raw.snapshot()["changes_done"]) == (False, 0)
    raw.update({"device": {"extruder": {"state": 1 | 1 << 19}}})
    assert (raw.snapshot()["busy"], raw.snapshot()["changes_done"]) == (True, 0)
    raw.update({"device": {"extruder": {"state": 1}}})
    assert (raw.snapshot()["busy"], raw.snapshot()["changes_done"]) == (False, 1)

    # legacy (P1): ams_status main = 1 while a filament change runs
    legacy = RawReport()
    legacy.update({"ams_status": 0x0102})
    assert legacy.snapshot()["busy"] is True
    legacy.update({"ams_status": 768})
    assert (legacy.snapshot()["busy"], legacy.snapshot()["changes_done"]) == (False, 1)


# --- notification fields: print_error, hms[] with first-seen times, cancel latch ---

CANCEL = 0x0300400C        # 50348044, what Bambu echoes in print_error on a user cancel


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _hms_key(entry):
    return entry["attr"], entry["code"]


@pytest.mark.parametrize("model", MODELS)
def test_notification_fields_from_fixtures(model, offline_collector):
    # Expected values read from the fixture independently: last value of each key wins.
    want = {"print_error": None, "gcode_state": None, "subtask_id": None, "job_attr": None, "hms": None}
    for line in kit.load_fixture(model):
        p = line["msg"]["print"]
        for key in ("print_error", "subtask_id", "job_attr"):
            if key in p:
                want[key] = p[key]
        if "gcode_state" in p:
            want["gcode_state"] = p["gcode_state"].upper()
        if "hms" in p:
            want["hms"] = [{"attr": h["attr"], "code": h["code"], "ts_unix": h.get("ts_unix"),
                            "ts_boot": h.get("ts_boot")} for h in p["hms"]]
    collector = offline_collector(model)
    for line in kit.load_fixture(model):
        kit.deliver(collector, line["msg"])
    snap = collector.client.get_device().forge_raw.snapshot()
    assert {k: snap[k] for k in ("print_error", "gcode_state", "subtask_id", "job_attr")} == \
        {k: want[k] for k in ("print_error", "gcode_state", "subtask_id", "job_attr")}
    assert [{k: h[k] for k in ("attr", "code", "ts_unix", "ts_boot")} for h in snap["hms"]] == want["hms"]
    assert all(isinstance(h["first_seen"], float) for h in snap["hms"])
    assert snap["print_error_since"] is None and snap["cancel_latched"] is False   # print_error 0 throughout


def test_notification_fields_fixture_anchors(offline_collector):
    # Literal values (fixtures/bambu/README.md, §1 of the notifications spec).
    snaps = {}
    for model in MODELS:
        collector = offline_collector(model)
        for line in kit.load_fixture(model):
            kit.deliver(collector, line["msg"])
        snaps[model] = collector.client.get_device().forge_raw.snapshot()
    assert {m: snaps[m]["job_attr"] for m in ("X1C", "P2S", "H2S")} == {"X1C": 18, "P2S": 18, "H2S": 18}
    assert snaps["P2S"]["subtask_id"] == ""            # an empty id is kept as sent
    assert snaps["P2S"]["fail_reason"] == "0" and snaps["P2S"]["err"] == "0" and snaps["P2S"]["mc_err"] == 0
    p2s = {_hms_key(h): h for h in snaps["P2S"]["hms"]}
    assert set(p2s) == {(0x05000600, 0x00020070)}     # 0500_0100_0003_0007 is gone at the end
    assert p2s[(0x05000600, 0x00020070)]["ts_unix"] == "20261003152025"
    assert p2s[(0x05000600, 0x00020070)]["ts_boot"] == 34642
    h2s = sorted(_hms_key(h) for h in snaps["H2S"]["hms"])
    assert h2s == [(0x03002E00, 0x00030001), (0x07002100, 0x00020025)]
    assert all(h["ts_unix"] is None and h["ts_boot"] is None for h in snaps["H2S"]["hms"])
    assert snaps["P1S"]["hms"] == [] and snaps["P1S"]["gcode_state"] == "RUNNING"


def test_flapping_hms_code_keeps_its_first_seen():
    # P2S capture: 0500_0100_0003_0007 leaves hms[] and comes back within seconds.
    flapper, steady = (0x05000100, 0x00030007), (0x05000600, 0x00020070)
    lines = kit.load_fixture("P2S")
    clock = _Clock()
    raw = RawReport(clock=clock)
    present = []
    for line in lines:
        clock.t = 1000.0 + line["t"]
        raw.update(line["msg"]["print"])
        keys = {_hms_key(h): h["first_seen"] for h in raw.snapshot()["hms"]}
        present.append((line["t"], flapper in keys, keys))
    arrivals = [cur[0] for prev, cur in zip([(None, False, None)] + present, present) if cur[1] and not prev[1]]
    first = arrivals[0]
    assert len(arrivals) == 2 and arrivals[1] - first > 20     # it did disappear and come back
    for t, here, keys in present:
        assert keys[steady] == 1000.0 + lines[0]["t"]
        if here:
            assert keys[flapper] == 1000.0 + first


def test_hms_first_seen_restarts_after_the_flap_grace():
    from app.collectors.bambu_raw import FLAP_GRACE_S
    code = {"attr": 0x07002500, "code": 0x00020001}
    clock = _Clock(0.0)
    raw = RawReport(clock=clock)

    def at(t, hms):
        clock.t = t
        raw.update({"hms": hms})
        return {_hms_key(h): h["first_seen"] for h in raw.snapshot()["hms"]}

    assert at(0, [code]) == {_hms_key(code): 0}
    assert at(10, []) == {}
    assert at(10 + FLAP_GRACE_S - 1, [code]) == {_hms_key(code): 0}      # back within the grace
    assert at(200, []) == {}
    assert at(250, [{"attr": 1, "code": 2}]) == {(1, 2): 250}             # the old code still pending
    assert at(200 + FLAP_GRACE_S, [code, {"attr": 1, "code": 2}]) == {_hms_key(code): 200 + FLAP_GRACE_S,
                                                                       (1, 2): 250}
    raw.update({"command": "push_status", "msg": 1, "mc_percent": 5})     # no hms key: the list is kept
    assert len(raw.snapshot()["hms"]) == 2
    # the same code twice in one report (live on P1/P3/P4): both copies kept, one time
    snap = at(400, [code, code])
    assert snap == {_hms_key(code): 200 + FLAP_GRACE_S} and len(raw.snapshot()["hms"]) == 2


def test_print_error_since_marks_each_new_code():
    clock = _Clock(5.0)
    raw = RawReport(clock=clock)
    raw.update({"print_error": 0})
    assert (raw.snapshot()["print_error"], raw.snapshot()["print_error_since"]) == (0, None)
    clock.t = 7.0
    raw.update({"print_error": 0x07008011})
    clock.t = 9.0
    raw.update({"print_error": 0x07008011})              # the same code: the time stays
    raw.update({"command": "push_status", "msg": 1})     # a delta without it: kept
    assert (raw.snapshot()["print_error"], raw.snapshot()["print_error_since"]) == (0x07008011, 7.0)
    clock.t = 12.0
    raw.update({"print_error": "50364513"})              # another code (a decimal string)
    assert (raw.snapshot()["print_error"], raw.snapshot()["print_error_since"]) == (50364513, 12.0)
    raw.update({"print_error": 0})
    assert (raw.snapshot()["print_error"], raw.snapshot()["print_error_since"]) == (0, None)


def test_cancel_latch_holds_for_the_failed_episode():
    raw = RawReport()
    latched = lambda: raw.snapshot()["cancel_latched"]
    raw.update({"gcode_state": "RUNNING", "print_error": 0})
    assert latched() is False
    raw.update({"gcode_state": "RUNNING", "print_error": CANCEL})   # the cancel report may still say RUNNING
    assert latched() is True
    raw.update({"gcode_state": "FAILED", "print_error": CANCEL})
    raw.update({"gcode_state": "FAILED", "print_error": 0})        # the printer clears the code
    raw.update({"gcode_state": "FAILED"})
    assert latched() is True and raw.snapshot()["print_error"] == 0
    raw.update({"gcode_state": "PREPARE"})                          # a new job
    assert latched() is False


@pytest.mark.parametrize("end", ["IDLE", "FINISH", "idle"])
def test_cancel_latch_ends_on_idle_or_finish(end):
    raw = RawReport()
    raw.update({"gcode_state": "RUNNING"})
    raw.update({"print_error": 0x0500400E})            # the other cancel code, alone (P1 delta)
    raw.update({"gcode_state": "FAILED"})
    assert raw.snapshot()["cancel_latched"] is True
    raw.update({"gcode_state": end})
    assert raw.snapshot()["cancel_latched"] is False


def test_cancel_latch_corner_cases():
    # a cancel from a pause latches, and a pause / resume is not a new job
    raw = RawReport()
    raw.update({"gcode_state": "PAUSE", "print_error": CANCEL})
    raw.update({"gcode_state": "RUNNING"})
    assert raw.snapshot()["cancel_latched"] is True
    # the very first report may be the cancel one: nothing was seen starting
    first = RawReport()
    first.update({"gcode_state": "RUNNING", "print_error": CANCEL})
    assert first.snapshot()["cancel_latched"] is True
    # a real failure code does not latch
    failed = RawReport()
    failed.update({"gcode_state": "RUNNING", "print_error": 0x03004000})
    failed.update({"gcode_state": "FAILED"})
    assert failed.snapshot()["cancel_latched"] is False
    # a stale cancel code at the start of the next job does not latch it
    stale = RawReport()
    stale.update({"gcode_state": "RUNNING", "print_error": CANCEL})
    stale.update({"gcode_state": "FAILED"})
    stale.update({"gcode_state": "PREPARE", "print_error": CANCEL})
    stale.update({"gcode_state": "RUNNING", "print_error": CANCEL})
    assert stale.snapshot()["cancel_latched"] is False


def test_notification_fields_ignore_garbage():
    raw = RawReport(clock=_Clock())
    raw.update({"gcode_state": "FAILED", "print_error": CANCEL, "subtask_id": "123", "job_attr": 18,
                "fail_reason": "0", "mc_err": 0, "mc_print_error_code": "0", "err": "0",
                "hms": [{"attr": 0x05000600, "code": 0x00020070, "ts_unix": "20261003152025", "ts_boot": 1}]})
    before = raw.snapshot()
    for junk in (
        {"print_error": "x"}, {"print_error": None}, {"print_error": True}, {"print_error": [1]},
        {"gcode_state": 5}, {"gcode_state": ""}, {"gcode_state": None},
        {"subtask_id": None}, {"subtask_id": [1]}, {"subtask_id": True}, {"job_attr": "x"}, {"job_attr": None},
        {"fail_reason": {"a": 1}}, {"err": None}, {"mc_err": [0]},
        {"hms": "x"}, {"hms": None}, {"hms": {"attr": 1}}, {"hms": [None, 3, {"attr": "x", "code": 1}, {"code": 5}]},
    ):
        raw.update(junk)      # never raises
    assert raw.snapshot() == before

    def boom():
        raise RuntimeError("clock")

    broken = RawReport(clock=boom)
    broken.update({"print_error": CANCEL, "hms": [{"attr": 1, "code": 2}]})   # still merged
    assert broken.snapshot()["print_error"] == CANCEL and broken.snapshot()["cancel_latched"] is True


def test_notification_snapshot_is_a_copy():
    raw = RawReport(clock=_Clock())
    raw.update({"hms": [{"attr": 1, "code": 2, "ts_unix": "x", "ts_boot": 3}]})
    snap = raw.snapshot()
    snap["hms"][0]["code"] = 99
    snap["hms"].append({})
    assert raw.snapshot()["hms"] == [{"attr": 1, "code": 2, "ts_unix": "x", "ts_boot": 3, "first_seen": 1000.0}]
