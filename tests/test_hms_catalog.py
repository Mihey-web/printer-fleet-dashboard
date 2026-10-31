"""Runtime Bambu HMS tables (app/services/hms_catalog, notifications spec step 3).

The table bodies below are synthetic and tiny (not Bambu's data); urlopen is
replaced by a fake for every test, and no_network fails a test that opens any
socket anyway.
"""
import gzip
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import pytest

from app.services import hms_catalog as hc

P = "Q7X"                     # synthetic serial prefix
PID = "bambu-1"
ROWS = [{"id": PID, "kind": "bambu", "serial": P + "0000TEST0001", "model": "P2S"}]
NOW = 1_800_000_000.0


def _actions(ver="101", data=None):
    return {"result": 0, "t": 1, "ver": ver, "data": data if data is not None else [
        {"ecode": "0C008001", "device": "default", "actions": [2, 5], "image": ""},
        {"ecode": "0C008001", "device": P, "actions": [27], "image": ""},           # model record: no merge
        {"ecode": "0C008002", "device": "default", "actions": [4, "6", True, 4], "image": ""},
        {"ecode": "0C00010000010001", "device": P, "actions": [29], "image": ""},
        {"ecode": "0C008003", "device": "ZZ1", "actions": [8], "image": ""},       # another model: ignored
        {"ecode": "junk", "device": "default", "actions": [1]},
    ]}


def _texts(ver="202", err=None, hms=None):
    return {"result": 0, "ver": ver, "data": {
        "device_error": {"ver": ver, "ru": err if err is not None else [
            {"ecode": "0C008001", "intro": " Тестовая пауза "},
            {"ecode": "0C008002", "intro": ""}]},
        "device_hms": {"ver": ver, "ru": hms if hms is not None else [
            {"ecode": "0C00010000010001", "intro": "Тестовый HMS"},
            {"ecode": "0C00010000020002", "intro": ""}]}}}


NOT_MODIFIED = {"result": 201, "ver": 0}


class _Resp:
    """A response read in parts, as http.client's (read1: what has arrived)."""

    def __init__(self, body, encoding=None):
        self.body = body
        self.pos = 0
        self.headers = {"Content-Encoding": encoding} if encoding else {}

    def read(self, n=-1):
        end = len(self.body) if n is None or n < 0 else self.pos + n
        out, self.pos = self.body[self.pos:end], min(end, len(self.body))
        return out

    read1 = read

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Net:
    """Fake urlopen: answers by endpoint, records every request."""

    def __init__(self):
        self.requests = []
        self.answer = {"action": _actions(), "text": _texts()}

    def __call__(self, req, timeout=None):
        assert isinstance(req, urllib.request.Request) and timeout == hc.TIMEOUT_S
        self.requests.append(req)
        kind = "action" if req.full_url.startswith(hc.ACTION_URL) else "text"
        a = self.answer[kind]
        if isinstance(a, Exception):
            raise a
        if isinstance(a, _Resp):
            return a
        return _Resp(a if isinstance(a, bytes) else json.dumps(a, ensure_ascii=False).encode("utf-8"))

    def queries(self):
        return [(r.full_url.split("?")[0], urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query))
                for r in self.requests]


@pytest.fixture(autouse=True)
def cat(tmp_path, monkeypatch, no_network):
    monkeypatch.setattr(hc, "CACHE_DIR", str(tmp_path / "hms"))
    monkeypatch.setattr(hc, "_index", {})
    monkeypatch.setattr(hc, "_meta", None)
    monkeypatch.setattr(hc, "_prefix_by_pid", {})
    net = _Net()
    monkeypatch.setattr(urllib.request, "urlopen", net)
    return net


def _restart(monkeypatch):
    monkeypatch.setattr(hc, "_index", {})
    monkeypatch.setattr(hc, "_meta", None)
    monkeypatch.setattr(hc, "_prefix_by_pid", {})


def _meta():
    with open(os.path.join(hc.CACHE_DIR, hc.META_FILE), encoding="utf-8") as f:
        return json.load(f)


# ---- parsing and lookups --------------------------------------------------

def test_the_model_record_wins_over_default_and_they_are_not_merged(cat):
    hc.refresh_once(NOW, ROWS)
    assert hc.action_ids(P, 0x0C008001) == ([27], True)
    assert hc.action_ids(P, "0C00_8002") == ([4, 6], True)          # default; digits, no bools or repeats
    assert hc.action_ids(P, "HMS_0C00_0100_0001_0001") == ([29], True)
    assert hc.action_ids(P, "0C00_8003") == ([], True)              # another model's record only
    assert hc.action_ids(P, "0C00_8FFF") == ([], True)              # not in the loaded table


def test_without_a_table_the_caller_falls_back(cat):
    assert hc.action_ids(P, "0C00_8001") == ([], False)
    assert hc.action_ids(None, "0C00_8001") == ([], False)
    assert hc.action_ids(P, "not a code") == ([], False)
    assert hc.text(P, "0C00_8001") is None


@pytest.mark.parametrize("code", ["0701_8004", "0701_8005", "0701_8007", "0701_8012", "0702_8012",
                                  "0703_8012", "07FF_8012", "07FF_8013"])
def test_studio_fixed_codes_get_retry_and_confirm(cat, code):
    assert hc.action_ids(None, code) == ([10002, 10004], True)      # no table needed
    cat.answer["action"] = _actions(data=[{"ecode": code.replace("_", ""), "device": P, "actions": [5]}])
    hc.refresh_once(NOW, ROWS)
    assert hc.action_ids(P, int(code.replace("_", ""), 16)) == ([10002, 10004], True)


def test_texts_by_code_length_and_empty_intro_is_none(cat):
    hc.refresh_once(NOW, ROWS)
    assert hc.text(P, "0C00_8001") == "Тестовая пауза"
    assert hc.text(P, 0x0C008001) == "Тестовая пауза"
    assert hc.text(P, "0c00_0100_0001_0001") == "Тестовый HMS"
    assert hc.text(P, "0C00_8002") is None                          # empty intro: Studio hides it
    assert hc.text(P, "0C00_0100_0002_0002") is None
    assert hc.text(P, "0C00_0100") is None
    assert hc.text("ZZ1", "0C00_8001") is None


# ---- download, cache, refresh ---------------------------------------------

def test_first_download_writes_the_cache_and_meta(cat):
    hc.refresh_once(NOW, ROWS)
    assert [q for _, q in cat.queries()] == [{"d": [P]}, {"lang": ["ru"], "d": [P]}]   # nothing cached: no v
    files = sorted(os.listdir(hc.CACHE_DIR))
    assert files == ["action_%s.json" % P, "meta.json", "text_ru_%s.json" % P]
    assert _meta() == {P: {"action_ver": "101", "text_ver": "202", "checked_at": NOW, "error": None}}


def test_cache_survives_a_restart_without_the_network(cat, monkeypatch):
    hc.refresh_once(NOW, ROWS)
    _restart(monkeypatch)
    cat.requests.clear()
    hc.load_cached(ROWS)
    assert cat.requests == []
    assert hc.text(P, "0C00_8001") == "Тестовая пауза"
    assert hc.action_ids(P, "0C00_8001") == ([27], True)
    hc.refresh_once(NOW + 3600, ROWS)                               # still fresh: no request
    assert cat.requests == []


def test_201_only_moves_checked_at(cat):
    hc.refresh_once(NOW, ROWS)
    before = sorted(os.listdir(hc.CACHE_DIR))
    cat.requests.clear()
    cat.answer = {"action": NOT_MODIFIED, "text": NOT_MODIFIED}
    later = NOW + hc.REFRESH_S
    hc.refresh_once(later, ROWS)
    assert [q for _, q in cat.queries()] == [{"d": [P], "v": ["101"]}, {"lang": ["ru"], "d": [P], "v": ["202"]}]
    assert hc.action_ids(P, "0C00_8001") == ([27], True)
    assert hc.text(P, "0C00_8001") == "Тестовая пауза"
    assert _meta()[P] == {"action_ver": "101", "text_ver": "202", "checked_at": later, "error": None}
    assert sorted(os.listdir(hc.CACHE_DIR)) == before


def test_a_new_version_replaces_the_table(cat):
    hc.refresh_once(NOW, ROWS)
    cat.answer = {"action": _actions("103", [{"ecode": "0C008001", "device": "default", "actions": [9]}]),
                  "text": NOT_MODIFIED}
    hc.refresh_once(NOW + hc.REFRESH_S, ROWS)
    assert hc.action_ids(P, "0C00_8001") == ([9], True)
    assert hc.text(P, "0C00_8001") == "Тестовая пауза"
    assert _meta()[P]["action_ver"] == "103"
    with open(os.path.join(hc.CACHE_DIR, "action_%s.json" % P), encoding="utf-8") as f:
        assert json.load(f)["ver"] == "103"


@pytest.mark.parametrize("answer,error", [
    (urllib.error.URLError("offline"), "URLError"),
    (TimeoutError("timed out"), "TimeoutError"),
    ({"result": 0, "ver": "9", "data": "nope"}, "BadResponse"),
    ({"result": 500}, "BadResponse"),
    (b"<html>not json</html>", "JSONDecodeError"),
])
def test_a_failed_refresh_keeps_the_old_tables_and_retries_next_pass(cat, answer, error):
    hc.refresh_once(NOW, ROWS)
    cat.answer = {"action": answer, "text": NOT_MODIFIED}
    later = NOW + hc.REFRESH_S
    hc.refresh_once(later, ROWS)
    assert hc.action_ids(P, "0C00_8001") == ([27], True)
    assert _meta()[P] == {"action_ver": "101", "text_ver": "202", "checked_at": NOW, "error": error}
    cat.requests.clear()
    hc.refresh_once(later + hc.LOOP_S, ROWS)                        # checked_at not moved: tried again
    assert len(cat.requests) == 2


def test_no_network_on_first_start_leaves_the_fallback(cat):
    cat.answer = {"action": urllib.error.URLError("offline"), "text": urllib.error.URLError("offline")}
    hc.refresh_once(NOW, ROWS)
    assert hc.action_ids(P, "0C00_8001") == ([], False)
    assert _meta()[P] == {"action_ver": None, "text_ver": None, "error": "URLError"}
    assert hc.status()[P]["error"] == "URLError"


def test_201_without_a_cached_table_is_an_error(cat):
    cat.answer = {"action": NOT_MODIFIED, "text": _texts()}
    hc.refresh_once(NOW, ROWS)
    assert hc.action_ids(P, "0C00_8001") == ([], False)
    assert hc.text(P, "0C00_8001") == "Тестовая пауза"
    assert _meta()[P]["error"] == "BadResponse" and "checked_at" not in _meta()[P]


def test_write_is_atomic(cat, monkeypatch):
    hc.refresh_once(NOW, ROWS)
    path = os.path.join(hc.CACHE_DIR, "action_%s.json" % P)
    with open(path, "rb") as f:
        old = f.read()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(hc.os, "replace", boom)
    cat.answer = {"action": _actions("104", []), "text": NOT_MODIFIED}
    hc.refresh_once(NOW + hc.REFRESH_S, ROWS)
    with open(path, "rb") as f:
        assert f.read() == old
    assert not [n for n in os.listdir(hc.CACHE_DIR) if n.endswith(".tmp")]
    assert hc.action_ids(P, "0C00_8001") == ([27], True)            # memory follows the disk


def test_a_broken_cache_file_is_downloaded_again(cat, monkeypatch):
    hc.refresh_once(NOW, ROWS)
    with open(os.path.join(hc.CACHE_DIR, "text_ru_%s.json" % P), "w", encoding="utf-8") as f:
        f.write("{broken")
    _restart(monkeypatch)
    hc.load_cached(ROWS)
    assert hc.text(P, "0C00_8001") is None and hc.action_ids(P, "0C00_8001") == ([27], True)
    cat.requests.clear()
    hc.refresh_once(NOW + 60, ROWS)                                 # fresh checked_at, but a table is missing
    assert [q for _, q in cat.queries()] == [{"d": [P], "v": ["101"]}, {"lang": ["ru"], "d": [P]}]
    assert hc.text(P, "0C00_8001") == "Тестовая пауза"


def test_a_clock_that_went_back_rechecks(cat):
    hc.refresh_once(NOW, ROWS)
    cat.requests.clear()
    cat.answer = {"action": NOT_MODIFIED, "text": NOT_MODIFIED}
    hc.refresh_once(NOW - 7 * 86400, ROWS)                          # fake-hwclock after a brownout
    assert len(cat.requests) == 2


def test_gzip_body_is_unpacked(cat):
    cat.answer["text"] = _Resp(gzip.compress(json.dumps(_texts()).encode("utf-8")), "gzip")
    hc.refresh_once(NOW, ROWS)
    assert hc.text(P, "0C00_8001") == "Тестовая пауза"


def test_oversized_body_is_refused(cat, monkeypatch):
    monkeypatch.setattr(hc, "MAX_BYTES", 64)
    cat.answer["text"] = _Resp(gzip.compress(json.dumps(_texts()).encode("utf-8")), "gzip")
    hc.refresh_once(NOW, ROWS)
    assert hc.text(P, "0C00_8001") is None
    assert _meta()[P]["error"] == "BadResponse"


class _Trickle(_Resp):
    """A byte at a time, each one second later on the fake clock."""

    def __init__(self, body, clock):
        super().__init__(body)
        self.clock = clock

    def read1(self, n=-1):
        self.clock[0] += 1.0
        return self.read(1)


def test_a_trickling_answer_is_cut_at_the_deadline(cat, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(hc, "_monotonic", lambda: clock[0])
    cat.answer["text"] = _Trickle(json.dumps(_texts()).encode("utf-8"), clock)
    hc.refresh_once(NOW, ROWS)
    assert hc.text(P, "0C00_8001") is None
    assert _meta()[P]["error"] == "BadResponse"
    assert clock[0] <= hc.DOWNLOAD_S + 2                   # never the whole body at a byte a second
    assert hc.action_ids(P, 0x0C008001) == ([27], True)    # the other table came in


def test_a_huge_record_parses_quickly_and_is_capped(cat):
    import time as _time
    many = list(range(1, 40001))
    cat.answer["action"] = _actions(data=[
        {"ecode": "0C008001", "device": P, "actions": many},                       # far too long: dropped
        {"ecode": "0C008002", "device": P, "actions": list(range(1, hc.MAX_ID_LIST + 1))},
        {"ecode": "0C008004", "device": P, "actions": ["7", "9" * 5000, 7, 8]}])   # no int() of 5000 digits
    cat.answer["text"] = _texts(err=[{"ecode": "0C008001", "intro": "Ы" * 50000}])
    t = _time.perf_counter()
    hc.refresh_once(NOW, ROWS)
    assert _time.perf_counter() - t < 2.0
    assert hc.action_ids(P, "0C00_8001") == ([], True)
    assert hc.action_ids(P, "0C00_8002") == (list(range(1, hc.MAX_IDS + 1)), True)
    assert hc.action_ids(P, "0C00_8004") == ([7, 8], True)
    assert hc.text(P, "0C00_8001") == "Ы" * hc.MAX_INTRO


# ---- what leaves the farm -------------------------------------------------

def test_only_the_prefix_lang_and_version_go_out(cat):
    serial = ROWS[0]["serial"]
    hc.refresh_once(NOW, ROWS)
    cat.answer = {"action": NOT_MODIFIED, "text": NOT_MODIFIED}
    hc.refresh_once(NOW + hc.REFRESH_S, ROWS)
    assert len(cat.requests) == 4
    for (base, q), req in zip(cat.queries(), cat.requests):
        assert base in (hc.ACTION_URL, hc.TEXT_URL) and base.startswith("https://e.bambulab.com/")
        assert set(q) <= {"d", "lang", "v"} and q["d"] == [P]
        assert serial not in req.full_url and serial[3:] not in req.full_url
        headers = {k.lower(): v for k, v in req.header_items()}
        assert headers["user-agent"] == "Forge-HMS/1"
        assert not {"cookie", "authorization"} & set(headers)


def test_prefixes_from_serial_then_model(cat):
    rows = [{"id": "b1", "kind": "bambu", "serial": "q7x0000test", "model": "X1C"},     # serial wins
            {"id": "b2", "kind": "bambu", "serial": "", "model": "P2S"},
            {"id": "b3", "kind": "bambu", "serial": "0!M000", "model": "a1 mini"},     # bad serial
            {"id": "b4", "kind": "bambu", "serial": None, "model": "Unknown"},
            {"id": "k1", "kind": "klipper", "serial": "ABC123", "model": "X1C"}]
    assert hc.collect_prefixes(rows) == {"b1": "Q7X", "b2": "22E", "b3": "030"}
    cat.answer = {"action": urllib.error.URLError("x"), "text": urllib.error.URLError("x")}
    hc.refresh_once(NOW, rows)
    assert hc.prefix_for("b1", "X1C") == "Q7X"
    assert hc.prefix_for("b4", "H2S") == "093"                      # no registry prefix: model fallback
    assert hc.prefix_for("nope", None) is None
    assert sorted({q["d"][0] for _, q in cat.queries()}) == ["030", "22E", "Q7X"]


def test_a_bad_prefix_never_builds_a_url():
    with pytest.raises(ValueError):
        hc._url("action", "../x", None)
    with pytest.raises(ValueError):
        hc._url("text", "Q7", None)


def test_status_per_prefix(cat):
    hc.refresh_once(NOW, ROWS + [{"id": "bambu-2", "kind": "bambu", "serial": P + "0000TEST0002"}])
    st = hc.status()
    assert st == {P: {"action_ver": "101", "text_ver": "202", "checked_at": NOW, "n_actions": 3,
                      "n_err": 2, "n_hms": 2, "error": None, "printers": 2}}
    assert ROWS[0]["serial"] not in json.dumps(st)


def test_start_runs_a_daemon_thread(monkeypatch):
    started = []

    class _T:
        def __init__(self, target=None, daemon=None, name=None):
            started.append((target, daemon, name))

        def start(self):
            pass

    monkeypatch.setattr(hc.threading, "Thread", _T)
    hc.start()
    assert started == [(hc._loop, True, "hms-catalog")]
