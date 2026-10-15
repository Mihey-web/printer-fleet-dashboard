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
