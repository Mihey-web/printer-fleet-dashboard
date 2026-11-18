"""Skipping parts mid-print (Bambu skip_objects): the job card's parts, the plate map, the command's guards."""
import io
import json
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from app.domain.models import PrinterState
from app.services import job_previews, print_material as pm, printer_commands as pc, project_summary as ps

XML = ('<config><plate><metadata key="index" value="1"/><metadata key="label_object_enabled" value="true"/>'
       '<object identify_id="491" name="Кубик.STL" skipped="false"/>'
       '<object identify_id="502" name="Кубик.STL" skipped="false"/>'
       '<object identify_id="524" name="Втулка.STL" skipped="false"/>'
       '<object identify_id="600" name="Скрытая.STL" skipped="true"/></plate></config>').encode()


def pick_png():
    image = Image.new("RGBA", (64, 64))
    for n, ident in enumerate((491, 502, 524)):
        color = (ident & 0xFF, (ident >> 8) & 0xFF, (ident >> 16) & 0xFF, 255)   # pixel = id as BGR
        image.paste(color, (4 + n * 20, 10, 16 + n * 20, 30))
    buf = io.BytesIO(); image.save(buf, format="PNG")
    return buf.getvalue()


def test_summary_lists_parts_and_saves_the_plate_map(tmp_path, monkeypatch):
    monkeypatch.setattr(job_previews, "DIRECTORY", tmp_path)
    def read(name):
        if name == "Metadata/pick_1.png":
            return pick_png()
        raise ValueError("err_no -2")
    out = []
    pm.collect_project(read, XML, None, out.append)
    s = out[0]
    assert s["parts"] == [{"id": 491, "name": "Кубик"}, {"id": 502, "name": "Кубик"}, {"id": 524, "name": "Втулка"}]
    assert s["skippable"] is True and job_previews.valid_key(s["pick"])
    with Image.open(tmp_path / (s["pick"] + ".png")) as im:   # cropped, colours (= ids) untouched
        ids = {b << 16 | g << 8 | r for r, g, b, a in im.convert("RGBA").getdata() if a}
    assert ids == {491, 502, 524}


def test_single_part_job_has_nothing_to_skip():
    xml = b'<config><plate><metadata key="index" value="1"/><metadata key="label_object_enabled" value="true"/><object identify_id="7" name="A" skipped="false"/></plate></config>'
    s = ps.bambu({"plate": None, "files": {"slice_info.config": pm.metadata_document(xml)}})
    assert "parts" not in s and "skippable" not in s


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(job_previews, "DIRECTORY", tmp_path)
    summary = ps.bambu({"plate": None, "files": {"slice_info.config": pm.metadata_document(XML)}})
    key = job_previews.save_project(summary)
    return lambda state="printing", skipped=None, project=key: NS(
        state=PrinterState(state), skipped_objects=skipped, project_key=project)


def test_skip_sends_only_new_parts_of_this_job(job):
    payload = pc.build_skip_objects({"objects": [502, 491]}, job(skipped=[491]))
    assert payload == {"print": {"sequence_id": "0", "command": "skip_objects", "obj_list": [502]}}
    assert pc.build_skip_objects({"objects": [524]}, job("paused"))["print"]["obj_list"] == [524]


@pytest.mark.parametrize("objects,status_kw,message", [
    ([], {}, pc.MSG_SKIP_NONE),
    ([True], {}, pc.MSG_SKIP_NONE),
    (["491"], {}, pc.MSG_SKIP_NONE),
    ([600], {}, pc.MSG_SKIP_UNKNOWN),          # hidden at slicing: not a part of the print
    ([999], {}, pc.MSG_SKIP_UNKNOWN),
    ([491], {"skipped": [491]}, pc.MSG_SKIP_DONE),
    ([491, 502, 524], {}, pc.MSG_SKIP_ALL),
    ([524], {"skipped": [491, 502]}, pc.MSG_SKIP_ALL),
    ([491], {"state": "idle"}, pc.STATE_MSG["pause_other"]),
    ([491], {"state": "finished"}, pc.STATE_MSG["pause_other"]),
    ([491], {"project": None}, pc.MSG_SKIP_NO_MAP),
])
def test_skip_refusals(job, objects, status_kw, message):
    with pytest.raises(ValueError) as e:
        pc.build_skip_objects({"objects": objects}, job(**status_kw))
    assert str(e.value) == message


def test_skip_needs_labelled_objects_and_live_status(job, tmp_path):
    xml = XML.replace(b'value="true"/>', b'value="false"/>', 1)
    key = job_previews.save_project(ps.bambu({"plate": None, "files": {"slice_info.config": pm.metadata_document(xml)}}))
    with pytest.raises(ValueError, match="маркировка"):
        pc.build_skip_objects({"objects": [491]}, job(project=key))
    with pytest.raises(ValueError):
        pc.build_skip_objects({"objects": [491]}, None)


def test_live_skipped_list_reaches_the_status():
    from app.services.normalizer import _skipped_objects
    assert _skipped_objects(NS(get_skipped_objects=[502, "491", 502])) == [491, 502]
    assert _skipped_objects(NS(get_skipped_objects=None)) is None and _skipped_objects(None) is None
    # the real vendored class: a property fed by the printer's s_obj
    from app.vendor.pybambu.models import PrintJob
    assert isinstance(PrintJob.__dict__["get_skipped_objects"], property)
