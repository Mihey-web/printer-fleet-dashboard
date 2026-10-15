"""Guards for the vendored pybambu (app/vendor/pybambu).

They pin what VENDORED.md promises: the package imports without its optional
dependencies, every local edit is marked and documented, and nothing that looks
like a LAN/mesh address ships in the vendored tree or the captured fixtures.
"""
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "app" / "vendor" / "pybambu"
FIXTURES = ROOT / "tests" / "fixtures" / "bambu"


def _run_python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)


# --- optional dependencies ----------------------------------------------------

def test_vendored_package_imports_without_bs4_and_pil():
    # bs4 (firmware page scrape) and Pillow (3MF pick image over FTP) serve
    # features the app keeps disabled; the PC running the tests has no bs4.
    r = _run_python("import sys; sys.modules['bs4'] = None; sys.modules['PIL'] = None; "
                    "import app.vendor.pybambu")
    assert r.returncode == 0, r.stderr


def test_vendored_package_import_does_not_load_bs4_or_pil():
    # Where both are installed (the Pi), importing must still not pull them in:
    # on top of what the package loads anyway they cost ~0.9 s of start-up and
    # ~12 MB of RSS there.
    r = _run_python("import sys, app.vendor.pybambu; "
                    "loaded = [m for m in ('bs4', 'PIL') if m in sys.modules]; "
                    "print(loaded); sys.exit(1 if loaded else 0)")
    assert r.returncode == 0, r.stdout + r.stderr


# --- local patches are marked and documented ------------------------------------

def _patch_markers() -> dict:
    counts = {}
    for path in sorted(VENDOR.rglob("*.py")):
        n = path.read_text(encoding="utf-8").count("# VENDOR PATCH:")
        if n:
            counts[path.relative_to(VENDOR).as_posix()] = n
    return counts


def _documented_patches() -> dict:
    text = (VENDOR / "VENDORED.md").read_text(encoding="utf-8")
    section = text.split("## Local patches", 1)[1].split("\n## ", 1)[0]
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r"^- `([\w/.]+\.py)` \((\d+) markers?\)", section, re.M)}


def test_vendor_patches_documented():
    assert _patch_markers() == _documented_patches()
    # Pin the patch set, so a re-sync that drops one is caught.
    assert _patch_markers() == {"utils.py": 2, "models.py": 3}


# --- public mirror: no LAN / mesh addresses -------------------------------------

_LAN_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # RFC 1918
    "100.64.0.0/10",                                   # CGNAT (mesh VPNs)
    "169.254.0.0/16",                                  # link-local
)]
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")


def test_vendored_tree_and_fixtures_carry_no_lan_addresses():
    # The repository is mirrored publicly. Examples use TEST-NET-1 (192.0.2.0/24).
    hits = []
    for base in (VENDOR, FIXTURES):
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".gz", ".pyc"):
                continue
            for m in _IPV4.finditer(path.read_text(encoding="utf-8", errors="replace")):
                try:
                    ip = ipaddress.ip_address(m.group(1))
                except ValueError:
                    continue
                if any(ip in net for net in _LAN_NETS):
                    hits.append(f"{path.relative_to(ROOT).as_posix()}: {ip}")
    assert hits == []


# --- frontend vocabulary ----------------------------------------------------------

def test_stage_labels_cover_vendored_stage_ids():
    # stage = CURRENT_STAGE_IDS slug; app.js shows an English humanized slug for
    # any key it has no Russian label for. idle/printing/unknown stay hidden.
    from app.vendor.pybambu.const import CURRENT_STAGE_IDS

    js = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const STAGE_LABELS = \{(.*?)\};", js, re.S).group(1)
    keys = set(re.findall(r"^\s*([a-z0-9_]+)\s*:", block, re.M))
    expected = set(CURRENT_STAGE_IDS.values()) - {"idle", "printing", "unknown"}
    assert sorted(expected - keys) == []


def _js_object_keys(name: str) -> set:
    js = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"var %s = \{(.*?)\};" % name, js, re.S)
    assert m, "%s not found in app.js" % name
    return set(re.findall(r"([a-z_]+)\s*:", m.group(1)))


def test_ams_ui_labels_cover_the_backend_slugs():
    # Every slug the backend sends for drying, filament changes and nozzles has
    # a Russian label in app.js (unknown ones would show nothing or "тип неизвестен").
    from app.services import ams_drying, bambu_extras

    assert set(ams_drying.DRY_STATUS.values()) <= _js_object_keys("DRY_STATUS_LABELS")
    assert set(ams_drying.DRY_PHASE.values()) <= _js_object_keys("DRY_PHASE_LABELS")
    steps = set(bambu_extras.FILAMENT_STEPS.values()) | set(bambu_extras.EXTERNAL_LOAD_STEPS.values())
    assert steps <= _js_object_keys("FILAMENT_STEP_LABELS")
    assert {"load", "unload"} <= _js_object_keys("FILAMENT_ACTION_LABELS")
    # The lib's nozzle slugs (Info._nozzle_type_name; high_flow_<material> is
    # labelled by prefix) and P1's raw values.
    nozzle_slugs = {"stainless_steel", "hardened_steel", "tungsten_carbide", "tpu_high_flow"}
    assert nozzle_slugs <= _js_object_keys("NOZZLE_TYPE_LABELS")


def _ams_unit_name_js() -> str:
    js = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
    assert "amsUnitName(u.unit_index)" in js           # the AMS card title uses it
    fn = re.search(r"function amsUnitName\(\w+\) \{.*?\n\s*\}", js, re.S)
    assert fn, "amsUnitName() not found in app.js"
    return fn.group(0)


def test_ams_card_names_units_by_physical_id():
    # unit_index is the physical unit id (history key): 0-3 for AMS / AMS 2 Pro /
    # AMS Lite, 128-135 for AMS HT. "AMS " + (id + 1) titled an HT unit "AMS 129".
    import json
    import shutil

    fn = _ams_unit_name_js()
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", fn + "\nconsole.log(JSON.stringify([0, 3, 128, 135].map(amsUnitName)));"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["AMS 1", "AMS 4", "AMS HT 1", "AMS HT 8"]


# --- nothing depends on PyPI pybambu any more --------------------------------------

_SKIP_DIRS = {".git", ".venv", "venv", "env", "build", "dist", "__pycache__", "node_modules"}


def _repo_python_files() -> list:
    try:
        out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True,
                             text=True, timeout=30, check=True).stdout
        files = [ROOT / line for line in out.splitlines() if line.strip()]
        if files:
            return files
    except (OSError, subprocess.SubprocessError):
        pass
    files = []
    for path in ROOT.rglob("*.py"):
        if not _SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            files.append(path)
    return files


def test_no_module_imports_pypi_pybambu():
    import ast

    offenders = []
    for path in _repo_python_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("app/vendor/") or not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name == "pybambu" or name.startswith("pybambu."):
                    offenders.append(f"{rel}:{node.lineno} {name}")
    assert offenders == []


def test_app_imports_with_pypi_pybambu_blocked():
    r = _run_python("import sys; sys.modules['pybambu'] = None; "
                    "import main, app.collectors.bambu_collector, app.services.normalizer, "
                    "app.services.printer_commands")
    assert r.returncode == 0, r.stderr


# --- packaging -------------------------------------------------------------------

def _requirement_names() -> dict:
    reqs = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].lower()
            reqs[name] = line
    return reqs


def test_requirements_have_no_pypi_pybambu():
    reqs = _requirement_names()
    assert "pybambu" not in reqs
    # The vendored lib's own imports (PyPI pybambu declared no dependencies).
    assert reqs.get("paho-mqtt") == "paho-mqtt==2.1.0"
    assert "requests" in reqs
    assert "python-dateutil" in reqs


def _spec_analysis_kwargs() -> dict:
    import ast

    tree = ast.parse((ROOT / "main.spec").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Analysis":
            return {kw.arg: kw.value for kw in node.keywords}
    raise AssertionError("Analysis(...) not found in main.spec")


def test_pyinstaller_spec_bundles_vendored_pybambu():
    import ast

    kwargs = _spec_analysis_kwargs()
    hidden = ast.literal_eval(kwargs["hiddenimports"])
    assert "app.vendor.pybambu" in hidden
    assert "pybambu" not in hidden
    datas = set(ast.literal_eval(kwargs["datas"]))
    # The lib finds these next to its own __file__ (sys._MEIPASS/app/vendor/pybambu/...).
    assert {
        ("app/vendor/pybambu/certs", "app/vendor/pybambu/certs"),
        ("app/vendor/pybambu/hms_error_text", "app/vendor/pybambu/hms_error_text"),
        ("app/vendor/pybambu/filaments.json", "app/vendor/pybambu"),
        ("app/vendor/pybambu/LICENSE", "app/vendor/pybambu"),
    } <= datas
