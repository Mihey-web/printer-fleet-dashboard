"""WCAG contrast of every theme's text and status colours on its own surfaces.

    python docs/dev/themes-2026-11/contrast.py [--md] [--css path/to/styles.css]

Reads the token blocks of app/web/styles.css (:root and html[data-theme="…"], and «Стекло»'s
prefers-color-scheme: light block as "glass-light"), resolves
var() references, lays the translucent surfaces over the page background (--bg, or
--glass-base for the glass theme) and prints the contrast ratio of each pair. AA is
4.5:1 for body text and 3:1 for large/bold text and UI parts (a status dot, a bar).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_css = sys.argv[sys.argv.index("--css") + 1] if "--css" in sys.argv else ROOT / "app" / "web" / "styles.css"
CSS = "\n" + Path(_css).read_text(encoding="utf-8")
THEMES = ["", "dark-classic", "light", "night", "cosmos", "amber", "glass", "glass-light"]
NAMES = {"": "Океан", "dark-classic": "Классика", "light": "Светлая", "night": "Ночь", "amber": "Янтарь",
         "cosmos": "Космос", "glass": "Стекло, тёмный режим", "glass-light": "Стекло, светлый режим"}
# the light pools painted by a theme's own rules (body::before), not by its tokens: the worst case
# for a translucent card is the pool under it, so they are laid over the page as well
WALLS = {
    "": ["rgba(45, 212, 191, 0.16)", "#0c3044"],
    "cosmos": ["rgba(124, 77, 255, 0.30)", "rgba(40, 90, 220, 0.22)", "#17113f"],
}


def block(sel):
    """Every block of this exact selector, joined (the --sev-pause tokens have their own)."""
    out, i = [], 0
    while True:
        i = CSS.find("\n" + sel + " {", i)
        if i < 0:
            return "\n".join(out)
        j, depth = CSS.index("{", i), 0
        for k in range(j, len(CSS)):
            depth += {"{": 1, "}": -1}.get(CSS[k], 0)
            if depth == 0:
                out.append(CSS[j + 1:k])
                break
        i = j


def decls(body):
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


def tokens(theme):
    d = decls(block("    :root"))
    if theme == "glass-light":
        # «Стекло» in the light: its dark block, then the prefers-color-scheme: light one
        d.update(decls(block('    html[data-theme="glass"]')))
        d.update(decls(block('      html[data-theme="glass"]')))
    elif theme:
        d.update(decls(block('    html[data-theme="%s"]' % theme)))
    return d


def resolve(d, v, depth=0):
    if depth > 10:
        return v
    return re.sub(r"var\((--[\w-]+)(?:,\s*([^)]+))?\)",
                  lambda m: resolve(d, d.get(m.group(1), m.group(2) or ""), depth + 1), v)


def rgba(v):
    v = v.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?", v)
    if m:
        h = m.group(1)
        a = int(m.group(2), 16) / 255 if m.group(2) else 1.0
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (a,)
    m = re.fullmatch(r"#([0-9a-fA-F]{3})", v)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1)) + (1.0,)
    m = re.fullmatch(r"rgba?\(([^)]+)\)", v)
    if m:
        parts = [p.strip() for p in m.group(1).replace("/", ",").split(",")]
        r, g, b = (float(p) for p in parts[:3])
        a = float(parts[3]) if len(parts) > 3 else 1.0
        return (r, g, b, a)
    raise ValueError(v)


def over(top, bottom):
    a = top[3]
    return tuple(top[i] * a + bottom[i] * (1 - a) for i in range(3)) + (1.0,)


def lum(c):
    def ch(x):
        x /= 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def ratio(a, b):
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def first_color(v):
    m = re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]+\)", v)
    return m.group(0) if m else None


def report(theme):
    d = tokens(theme)
    get = lambda n: rgba(first_color(resolve(d, d[n])))
    base = get("--glass-base") if "--glass-base" in d else get("--bg")
    walls = [base]
    if "--glass-wall" in d:
        # the glass wallpaper: every light pool may lie under the pane
        walls += [over(rgba(c), base) for c in re.findall(r"rgba\([^)]+\)|#[0-9a-fA-F]{6}", d["--glass-wall"])]
    walls += [over(rgba(c), base) for c in WALLS.get(theme, [])]
    rows = []
    for fg in ("--text", "--text-dim", "--text-muted", "--info", "--ok", "--warn", "--danger", "--sev-pause-text"):
        if fg not in d:
            continue
        worst = None
        for wall in walls:
            panel = over(get("--bg-panel"), wall)      # a card over this spot of the page
            soft = over(get("--bg-soft"), panel)       # a chip on that card
            c = get(fg)
            r = tuple(ratio(over(c, bg) if c[3] < 1 else c, bg) for bg in (panel, soft, wall))
            worst = r if worst is None else tuple(min(a, b) for a, b in zip(worst, r))
        rows.append((fg,) + worst)
    return rows


def main():
    md = "--md" in sys.argv
    # the --sev-pause tokens live in their own one-line blocks
    for th in THEMES:
        print(("### " if md else "== ") + NAMES[th] + (" (" + th + ")" if th else " (:root)"))
        try:
            rows = report(th)
        except Exception as e:  # a theme not there yet
            print("  —", e)
            continue
        if md:
            print("| токен | на карточке | на чипе | на фоне |\n|---|---|---|---|")
        for fg, a, b, c in rows:
            mark = lambda r: ("%.1f" % r) + ("" if r >= 4.5 else " ⚠" if r >= 3 else " ✗")
            if md:
                print("| `%s` | %s | %s | %s |" % (fg, mark(a), mark(b), mark(c)))
            else:
                print("  %-18s card %-7s chip %-7s page %s" % (fg, mark(a), mark(b), mark(c)))


if __name__ == "__main__":
    main()
