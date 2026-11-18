"""Skipping parts mid-print on Creality: the reqPrintObjects reply, the plate map's data, the guards, the frames."""
import json

import pytest

from app.api.admin import PrinterCommandRequest
from app.domain.models import PrinterKind
from app.services import creality_parts as cp
from app.services.normalizer import normalize_ws_dict

# The shape K2 Plus / K1C / Ender-5 Max send (16.11.2026): JSON text with spaces, Klipper's
# upper-case names; the names here are made up (real print names stay off the repo).
SQUARE = [[125.7, 91.03], [174.7, 91.03], [174.7, 129.03], [125.7, 129.03], [125.7, 91.03]]
REPLY = {
    "objects": json.dumps([
        {"name": "BRACKET.STL_ID_0_COPY_0", "center": [150.2, 110.0], "polygon": SQUARE},
        {"name": "BRACKET.STL_ID_1_COPY_0", "center": [200.2, 110.0],
         "polygon": [[x + 50, y] for x, y in SQUARE]},
        {"name": "LID_V2.STL_ID_2_COPY_0", "center": [199.8, 218.5],
         "polygon": [[x + 50, y + 100] for x, y in SQUARE]},
    ], indent=1),
    "excluded_objects": "[ ]",
    "current_object": "BRACKET.STL_ID_1_COPY_0",
}
NAMES = ["BRACKET.STL_ID_0_COPY_0", "BRACKET.STL_ID_1_COPY_0", "LID_V2.STL_ID_2_COPY_0"]


def live(**over):
    data = dict(REPLY, **over)
    return cp.Reader().absorb(data)


def test_reply_parses_into_parts_with_outlines():
    d = live()
    assert [p["name"] for p in d["print_objects"]] == NAMES
    assert d["print_objects"][0]["polygon"][1] == [174.7, 91.03] and d["print_objects"][0]["center"] == [150.2, 110.0]
    assert d["excluded_objects"] == [] and d["current_object"] == NAMES[1]


@pytest.mark.parametrize("raw,expected", [
    ("[ ]", []),
    ("not json", None),
    ('{"a": 1}', None),
    ('[{"name": ""}, {"name": 5}, "x", {"name": "A", "polygon": [[1, 2]]}, {"name": "A"}]',
     [{"name": "A", "polygon": None, "center": None}]),          # bad entries out, a 1-point outline is none
    ('[{"name": "B", "polygon": [[0, 0], [1, "x"], [1, 1], [true, 2], [0, 1]], "center": [NaN, 1]}]',
     [{"name": "B", "polygon": [[0.0, 0.0], [1.0, 1.0], [0.0, 1.0]], "center": None}]),
])
def test_objects_parsing_is_defensive(raw, expected):
    assert cp.parse_objects(raw) == expected


def test_excluded_list_is_names():
    assert cp.parse_excluded('[ "A", "", 3, "B" ]') == ["A", "B"]
    assert cp.parse_excluded("garbage") is None


def test_reader_parses_only_when_the_text_changes(monkeypatch):
    reader, calls = cp.Reader(), []
    real = cp.parse_objects
    monkeypatch.setattr(cp, "parse_objects", lambda v: calls.append(v) or real(v))
    reader.absorb(REPLY)
    reader.absorb(REPLY)
    assert len(calls) == 1
    out = reader.absorb(dict(REPLY, objects="[ ]"))
    assert len(calls) == 2 and out["print_objects"] == []


def test_labels_drop_the_slicer_suffix_and_number_repeats():
    assert cp.labels(live()["print_objects"]) == ["BRACKET №1", "BRACKET №2", "LID V2"]


def test_view_is_the_plate_map():
    d = live(excluded_objects='[ "BRACKET.STL_ID_0_COPY_0", "OTHER_JOB_PART" ]')
    v = cp.view(d, "k2plus")
    assert v["bed"] == [350, 350] and v["current"] == NAMES[1]
    assert [p["id"] for p in v["parts"]] == NAMES and v["parts"][2]["name"] == "LID V2"
    assert v["skipped"] == ["BRACKET.STL_ID_0_COPY_0"]          # only this job's parts
    assert cp.view(d, None)["bed"] is None
    assert cp.view({}, "k1c") == {"parts": [], "skipped": [], "current": None, "bed": [220, 220]}


def test_status_reports_skipped_parts_by_name():
    data = dict(live(excluded_objects='[ "LID_V2.STL_ID_2_COPY_0" ]'), state="printing", progress=40,
                remaining_time=600, print_time=400)
    st = normalize_ws_dict("c1", "K2", PrinterKind.CREALITY, data, device_type="k2plus")
    assert st.skipped_objects == ["LID_V2.STL_ID_2_COPY_0"]
    assert st.to_dict()["skipped_objects"] == ["LID_V2.STL_ID_2_COPY_0"]
    assert normalize_ws_dict("c1", "K2", PrinterKind.CREALITY, {"state": "idle"}).skipped_objects is None
    assert normalize_ws_dict("k", "V", PrinterKind.KLIPPER, dict(data)).skipped_objects is None


def test_skip_frames_one_name_each_in_the_jobs_order():
    assert cp.skip_frames([NAMES[2], NAMES[0]], live()) == [
        {"excludeObjects": [NAMES[0]]}, {"excludeObjects": [NAMES[2]]}]
    # already skipped ones are left out
    d = live(excluded_objects=json.dumps([NAMES[0]]))
    assert cp.skip_frames([NAMES[0], NAMES[1]], d) == [{"excludeObjects": [NAMES[1]]}]


@pytest.mark.parametrize("names,over,message", [
    ([], {}, cp.MSG_NONE),
    ("A", {}, cp.MSG_NONE),
    ([3], {}, cp.MSG_NONE),
    ([""], {}, cp.MSG_NONE),
    (["x" * 201], {}, cp.MSG_NONE),
    ([NAMES[0]], {"objects": "[ ]"}, cp.MSG_UNLABELED),           # no labels in the G-code
    (["SOMETHING_ELSE"], {}, cp.MSG_UNKNOWN),
    ([NAMES[0]], {"excluded_objects": json.dumps([NAMES[0]])}, cp.MSG_DONE),
    (NAMES, {}, cp.MSG_ALL),
    ([NAMES[2]], {"excluded_objects": json.dumps(NAMES[:2])}, cp.MSG_ALL),
])
def test_skip_refusals(names, over, message):
    with pytest.raises(ValueError) as e:
        cp.skip_frames(names, live(**over))
    assert str(e.value) == message


def test_command_request_takes_ids_or_names():
    assert PrinterCommandRequest(action="skip_objects", objects=[491, 502]).objects == [491, 502]
    assert PrinterCommandRequest(action="skip_objects", objects=NAMES).objects == NAMES
    for bad in ([True], [1.5], [""], ["x" * 201]):
        with pytest.raises(ValueError):
            PrinterCommandRequest(action="skip_objects", objects=bad)


def test_parts_endpoint_serves_the_live_map_to_a_viewer(monkeypatch):
    from types import SimpleNamespace as NS
    from fastapi.testclient import TestClient
    import config as cfg
    from app.api import auth as api_auth, main as api_main
    from app.domain.models import PrinterState, PrinterStatus
    from app.domain.user_models import UserRole
    from app.services import auth_service

    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("viewer", UserRole.VIEWER))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    statuses = {"c1": PrinterStatus(id="c1", label="K2", kind=PrinterKind.CREALITY, online=True,
                                    state=PrinterState.PRINTING, device_type="k2plus"),
                "b1": PrinterStatus(id="b1", label="X1", kind=PrinterKind.BAMBU, online=True, state=PrinterState.PRINTING)}
    snapshot = {"c1": live()}

    class Store:
        def get_one(self, pid):
            return statuses.get(pid)

        def get_all(self):
            return list(statuses.values())

        def get_collector(self, pid):
            return NS(client=NS(get_data=lambda: dict(snapshot.get(pid) or {})))

    client = TestClient(api_main.create_app(Store()))
    client.cookies.set("access_token", "stub")
    r = client.get("/api/printers/c1/parts")
    assert r.status_code == 200
    body = r.json()
    assert [p["id"] for p in body["parts"]] == NAMES and body["bed"] == [350, 350] and body["current"] == NAMES[1]
    assert client.get("/api/printers/b1/parts").json()["parts"] == []
    snapshot["c1"] = {}                                                   # WS down
    assert client.get("/api/printers/c1/parts").json()["parts"] == []
    assert client.get("/api/printers/nope/parts").status_code == 404
