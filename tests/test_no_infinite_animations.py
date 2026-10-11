"""Guard: the dashboard must not run endless CSS animations while idle.

Any infinite animation keeps Chrome producing frames, and every frame
re-composites the whole page (backdrop-filter blurs on the cards plus the
full-screen mix-blend noise layer). Measured 12.10.2026 on the owner's PC with
a 7 px pulsing dot: GPU 13-19 % and ~130-170 % CPU in Chrome's GPU process on
an otherwise idle tab; without animations 0.1 % / 1.5 %. Cards are re-created
whenever their markup changes, so a "pulse N times" animation would restart
every poll too — state is shown with static styling instead.

Only the loading spinner may spin forever: it is visible just while a request
is in flight.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
ALLOWED = {"spin"}

ANIMATION_DECL = re.compile(r"animation\s*:\s*([^;}\"'`]+)", re.I)
ITERATION_DECL = re.compile(r"animation-iteration-count\s*:\s*infinite", re.I)


def _web_sources():
    return sorted(p for p in WEB.rglob("*") if p.suffix in (".css", ".html", ".js"))


def test_web_sources_found():
    assert any(p.suffix == ".css" for p in _web_sources())


def test_no_infinite_animations_except_spinner():
    offenders = []
    for path in _web_sources():
        text = path.read_text(encoding="utf-8")
        for m in ANIMATION_DECL.finditer(text):
            value = m.group(1)
            if "infinite" not in value.lower():
                continue
            # Whole identifier tokens only: "0.7s" or "steps(4," are not names.
            names = {t for t in value.split() if re.fullmatch(r"[A-Za-z_][\w-]*", t)} - {
                "infinite", "linear", "ease", "ease-in", "ease-out", "ease-in-out",
                "alternate", "reverse", "normal", "both", "forwards", "backwards", "steps"}
            if not names <= ALLOWED:
                line = text.count("\n", 0, m.start()) + 1
                offenders.append("%s:%d: %s" % (path.relative_to(ROOT), line, value.strip()))
        for m in ITERATION_DECL.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append("%s:%d: animation-iteration-count: infinite" % (path.relative_to(ROOT), line))
    assert not offenders, "infinite animations burn GPU on an idle tab:\n" + "\n".join(offenders)


def test_guard_catches_a_pulse(tmp_path):
    # Sanity check of the regex on a typical offender.
    m = ANIMATION_DECL.search(".dot { animation: livePulse 2.2s infinite; }")
    assert m and "infinite" in m.group(1)
