import io
import zipfile
import pytest
from PIL import Image
from app.services import job_previews as previews, print_material as pm

def picture():
    image = Image.new('RGBA', (100, 100))
    image.paste((255, 200, 0, 255), (30, 20, 70, 80))
    buf = io.BytesIO(); image.save(buf, format='PNG')
    return buf.getvalue()

def test_crop_deduplicate_and_reject_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(previews, 'DIRECTORY', tmp_path)
    key = previews.save(picture())
    assert previews.save(picture()) == key
    assert len(list(tmp_path.iterdir())) == 1
    with Image.open(tmp_path / (key + '.png')) as image:
        assert image.size == (40, 60)
    for data in (b'', b'bad', b'x' * (previews.MAX_BYTES + 1)):
        with pytest.raises(Exception): previews.save(data)
    assert not previews.valid_key('../secret')

def test_preview_selects_exact_plate_and_does_not_break_weight():
    xml = b'<config><plate><metadata key="index" value="1"/><metadata key="weight" value="10"/></plate><plate><metadata key="index" value="2"/><metadata key="weight" value="20"/></plate></config>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        archive.writestr('Metadata/slice_info.config', xml)
        archive.writestr('Metadata/plate_1.png', b'one')
        archive.writestr('Metadata/plate_2.png', b'two')
    with zipfile.ZipFile(buf) as archive:
        images = []
        assert pm.archive_grams(archive, 2, images.append) == 20
        assert images == [b'two']
        assert pm.preview_plate(xml, None) is None
        def fail(data): raise ValueError('invalid image')
        assert pm.archive_grams(archive, 2, fail) == 20

def test_old_job_preview_cannot_replace_new_job(tmp_path, monkeypatch):
    monkeypatch.setattr(previews, 'DIRECTORY', tmp_path)
    reader = pm.MaterialReader()
    old = (1, 'old', 'old', 'old.3mf')
    reader.key = (2, 'new', 'new', 'new.3mf')
    def read(*args, on_preview, **kwargs):
        on_preview(picture())
        return 100
    monkeypatch.setattr(pm, 'read_bambu', read)
    reader._read(None, old)
    assert reader.preview is None
    assert reader.value is None

def test_project_summary_from_3mf_metadata(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(previews, 'DIRECTORY', tmp_path)
    xml = ('<config><header><header_item key="X-BBL-Client-Version" value="02.08.02.61"/></header>'
           '<plate><metadata key="index" value="1"/></plate>'
           '<plate><metadata key="index" value="2"/><metadata key="prediction" value="600"/>'
           '<metadata key="weight" value="25.5"/><metadata key="nozzle_diameters" value="0.4"/>'
           '<filament id="1" type="PETG" color="#FFFF00FF" used_g="25.5" used_m="8.4"/>'
           '<object identify_id="8" name="Кубик.STL" skipped="false"/>'
           '<object identify_id="9" name="Кубик.STL" skipped="true"/></plate></config>').encode()
    def read(name):
        if name.endswith('project_settings.config'):
            return json.dumps({"layer_height": "0.2", "sparse_infill_density": "15%", "sparse_infill_pattern": "grid",
                               "wall_loops": "2", "enable_support": "1", "support_type": "tree(auto)",
                               "curr_bed_type": "Textured PEI Plate", "textured_plate_temp": ["70"],
                               "nozzle_temperature": ["255"], "filament_settings_id": ["Generic PETG"]}).encode()
        raise FileNotFoundError(name)
    summaries = []
    pm.collect_project(read, xml, 2, summaries.append)
    s = summaries[0]
    assert s['title'] == 'Кубик' and s['plate'] == 2 and s['time_s'] == 600 and s['grams'] == 25.5
    assert s['filaments'] == [{'type': 'PETG', 'color': '#FFFF00', 'grams': 25.5, 'meters': 8.4,
                               'profile': 'Generic PETG', 'nozzle_temp': 255.0}]
    assert s['objects'] == [{'name': 'Кубик', 'count': 2, 'skipped': 1}]
    assert (s['layer_mm'], s['infill'], s['infill_pattern'], s['supports']) == (0.2, '15%', 'сетка', 'древовидные, авто')
    assert (s['bed'], s['bed_temp'], s['slicer']) == ('текстурированный PEI', 70.0, 'Bambu Studio 2.8.2')
    key = previews.save_project(s)
    assert previews.save_project(s) == key and len(list(tmp_path.glob('*.json'))) == 1
    with pytest.raises(ValueError): pm.metadata_document(b'<!DOCTYPE config><config/>')

def test_project_summary_without_print_settings_and_legacy_files():
    from app.services import project_summary as ps
    xml = b'<config><plate><metadata key="index" value="1"/><metadata key="support_used" value="false"/><filament id="1" type="PLA" used_g="3"/></plate></config>'
    summaries = []
    pm.collect_project(lambda name: (_ for _ in ()).throw(ValueError('err_no -2')), xml, None, summaries.append)
    assert summaries[0]['supports'] == 'нет' and 'layer_mm' not in summaries[0] and 'plate' not in summaries[0]
    legacy = {'plate': 1, 'files': {'slice_info.config': pm.metadata_document(xml)}, 'unavailable': []}
    assert ps.from_stored(legacy) == summaries[0]

def test_creality_summary_and_names():
    from app.services import project_summary as ps
    item = {"name": "Колесо 2 V.2 120мм.STL_PETG_8h55m7s.gcode", "timeCost": 32107, "consumables": 117402,
            "modelX": 9940, "modelY": 17194, "modelZ": 22800, "material": "PETG", "nozzleTemp": 25000,
            "bedTemp": 7000, "materialColors": "#FFF144", "filamentWeight": "358.63",
            "nozzle_diameter_from_file": 0.4, "printer_software_from_file": "Creality_Print"}
    s = ps.creality(item)
    assert s['title'] == 'Колесо 2 V.2 120мм' and s['time_s'] == 32107 and s['grams'] == 358.63
    assert s['filaments'] == [{'type': 'PETG', 'color': '#FFF144', 'grams': 358.63, 'meters': 117.402}]
    assert (s['nozzle_temp'], s['bed_temp'], s['size_mm'], s['slicer']) == (250.0, 70.0, [99.4, 171.94, 228.0], 'Creality Print')
    assert ps.short_name('/data/Шестерня 20 мм.gcode.3mf') == 'Шестерня 20 мм'
    assert all(ps.generic_name(n) for n in ('project_file', '/data/Metadata/plate_1.gcode', '', None)
               ) and not ps.generic_name('/sdcard/plate_1.gcode')
    assert not ps.generic_name('Кубик')


def _card(summary, title, planned=False, grams=""):
    """The job card's body as preview.js renders it (node, no DOM)."""
    import json, shutil, subprocess
    from pathlib import Path
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    src = ("globalThis.window = {}; globalThis.document = {addEventListener() {}};\n"
           + Path("app/web/preview.js").read_text(encoding="utf-8")
           + "\nconsole.log(JSON.stringify(window.ForgePreview.markup(%s, %s, %s, %s)));"
           % (json.dumps(summary, ensure_ascii=False), json.dumps(title, ensure_ascii=False), json.dumps(planned),
              json.dumps(grams, ensure_ascii=False)))
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


P1S_JOB = {"title": "Колесо 2 V.2 120мм", "time_s": 37904, "grams": 352.54,
           "filaments": [{"type": "PETG", "color": "#FFF144", "grams": 352.54, "meters": 117.26,
                          "profile": "Bambu PETG Basic @BBL X1C", "nozzle_temp": 255.0}],
           "objects": [{"name": "Колесо 2 V.2 120мм", "count": 4}], "nozzle_mm": 0.4, "layer_mm": 0.28,
           "first_layer_mm": 0.2, "infill": "15%", "infill_pattern": "сетка", "walls": 4,
           "top_layers": 4, "bottom_layers": 3, "brim": "авто",   # a summary stored before 17.11
           "supports": "обычные, авто", "support_grams": 4.4, "bed": "текстурированный PEI", "bed_temp": 70.0,
           "nozzle_temp": 255.0, "print_profile": "0.28mm Extra Draft @BBL X1C",   # stored before 17.11
           "slicer": "Bambu Studio 2.9.1"}


def test_job_card_shows_each_fact_once():
    html = _card(P1S_JOB, "Колесо 2 V.2 120мм 4 шт")
    for gone in ("Кайма", "Слоёв сверху", "Профиль печати", "Extra Draft", "@BBL", "Температура",
                 "<ul class=\"pj-objects\">"):
        assert gone not in html, gone
    assert html.count("255°") == 1 and "117,3 м" in html                       # the nozzle: in the filament row only
    assert html.count("352,5") == 1 and html.startswith("<h3>Пластик <small>352,5 г</small></h3>")   # the weight: by «Пластик»
    shown = _card(P1S_JOB, "Колесо", grams="≈ 352,5 г")                            # the printer card's own text
    assert shown.startswith("<h3>Пластик <small>≈ 352,5 г</small></h3>") and shown.count("352,5") == 1
    rows = html.split('<ul class="pj-filaments">')[1].split("</ul>")[0]
    assert "<strong>Bambu PETG Basic</strong>" in rows and "<small>" not in rows
    assert "<span>текстурированный PEI, 70°</span>" in html
    assert "<span>обычные, авто · 4,4 г</span>" in html
    assert "Детали на столе <small>4 штуки</small>" in html and "Время по расчёту" in html
    assert "Время по расчёту" not in _card(P1S_JOB, "Колесо", planned=True)       # the history's head has it


def test_job_card_lists_parts_only_when_they_add_something():
    one = dict(P1S_JOB, objects=[{"name": "Шестерня 20 мм", "count": 1}])
    assert "Детали на столе" not in _card(one, "Шестерня 20 мм")
    assert "<span>Шестерня 20 мм</span>" in _card(one, "Задание 7")
    assert "Детали на столе" not in _card(dict(one, objects=[{"name": "1", "count": 1}]), "Корпус 20 мм")
    two = dict(P1S_JOB, filaments=P1S_JOB["filaments"] * 2,
               objects=[{"name": "Кубик", "count": 2, "skipped": 1}, {"name": "Корпус", "count": 1}])
    html = _card(two, "Кубик")
    assert "<span>Кубик</span><b>×2</b><em>пропущено 1</em>" in html and "<span>Корпус</span>" in html
    assert html.count("352,5 г") == 3 and html.count("255°") == 2         # the total, then each filament's own


def test_creality_card_keeps_the_nozzle_temperature():
    from app.services import project_summary as ps
    s = ps.creality({"name": "Деталь.gcode", "material": "PETG", "nozzleTemp": 25000, "bedTemp": 7000,
                     "filamentWeight": "12.5", "materialColors": "#FFF144"})
    html = _card(s, "Деталь")
    assert "250°" in html and "<span>70°</span>" in html and html.count("12,5 г") == 1   # by «Пластик» only
