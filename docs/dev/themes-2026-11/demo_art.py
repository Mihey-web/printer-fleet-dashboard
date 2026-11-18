"""Pictures for the demo farm, drawn with Pillow: a slicer's plate preview (parts standing on
an isometric build plate), the plate map of «Пропустить деталь» (each part filled with its id)
and the printer's own file thumbnails. No text in any picture; every picture is the same on
every run (no randomness here: the layouts come from the catalogue).

A part is a list of solids: a footprint polygon (plate millimetres) extruded from z0 by h,
in one colour, with holes drawn as dark wells on its top face.
"""
import io
import math

from PIL import Image, ImageDraw, ImageFilter

COS30, SIN30 = math.cos(math.radians(30)), 0.5
PLATE_MM = 200.0


# --- colours -------------------------------------------------------------------------------

def rgb(color):
    c = str(color).lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def shade(c, f):
    return tuple(max(0, min(255, int(round(v * f)))) for v in c)


def lift(c, t):
    """Towards white by t (0..1): the top faces and the edges' highlight."""
    return tuple(int(round(v + (255 - v) * t)) for v in c)


# --- footprints ----------------------------------------------------------------------------

def _rot(points, cx, cy, angle):
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)
    return [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in points]


def rect(cx, cy, w, d, angle=0.0):
    return _rot([(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)], cx, cy, angle)


def rounded(cx, cy, w, d, r, angle=0.0, n=6):
    r = min(r, w / 2, d / 2)
    pts = []
    for qx, qy, start in ((w / 2 - r, -d / 2 + r, -90), (w / 2 - r, d / 2 - r, 0),
                          (-w / 2 + r, d / 2 - r, 90), (-w / 2 + r, -d / 2 + r, 180)):
        for i in range(n + 1):
            a = math.radians(start + 90 * i / n)
            pts.append((qx + r * math.cos(a), qy + r * math.sin(a)))
    return _rot(pts, cx, cy, angle)


def circle(cx, cy, r, n=40):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def polygon_n(cx, cy, r, n, phase=0.0):
    return [(cx + r * math.cos(phase + 2 * math.pi * i / n), cy + r * math.sin(phase + 2 * math.pi * i / n))
            for i in range(n)]


def gear(cx, cy, r, teeth, depth):
    pts = []
    steps = teeth * 4
    for i in range(steps):
        a = 2 * math.pi * i / steps
        rr = r if (i % 4) in (1, 2) else r - depth
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


def arc_band(cx, cy, r_out, r_in, a0, a1, n=28):
    out = [(cx + r_out * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
            cy + r_out * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]
    inner = [(cx + r_in * math.cos(math.radians(a1 - (a1 - a0) * i / n)),
              cy + r_in * math.sin(math.radians(a1 - (a1 - a0) * i / n))) for i in range(n + 1)]
    return out + inner


def ell(cx, cy, s, t, angle=0.0):
    """An L footprint s × s, legs t wide."""
    pts = [(0, 0), (s, 0), (s, t), (t, t), (t, s), (0, s)]
    return _rot([(x - s / 2, y - s / 2) for x, y in pts], cx, cy, angle)


def solid(poly, h, color, z0=0.0, holes=()):
    return {"poly": poly, "h": h, "z0": z0, "color": color, "holes": list(holes)}


# --- parts: one builder per shape, (cx, cy) the cell's centre, s its size ---------------------

def p_bracket(cx, cy, s, colors, angle=0.0):
    c = colors[0]
    k = s * 0.92
    t = k * 0.28
    poly = ell(cx, cy, k, t, angle)
    holes = [circle(*_rot([(-k / 2 + t / 2, k * 0.18)], cx, cy, angle)[0], t * 0.22, 18),
             circle(*_rot([(k * 0.18, -k / 2 + t / 2)], cx, cy, angle)[0], t * 0.22, 18)]
    return [solid(poly, s * 0.26, c, holes=holes)]


def p_gear(cx, cy, s, colors, angle=0.0):
    return [solid(gear(cx, cy, s * 0.47, 14, s * 0.07), s * 0.2, colors[0], holes=[circle(cx, cy, s * 0.11, 24)])]


def p_case(cx, cy, s, colors, angle=0.0):
    return [solid(rounded(cx, cy, s * 0.95, s * 0.68, s * 0.1, angle), s * 0.42, colors[0],
                  holes=[rounded(cx, cy, s * 0.82, s * 0.55, s * 0.06, angle)])]


def p_case_lid(cx, cy, s, colors, angle=0.0):
    c = colors[1] if len(colors) > 1 else colors[0]
    return [solid(rounded(cx, cy, s * 0.95, s * 0.68, s * 0.1, angle), s * 0.07, c,
                  holes=[circle(cx + s * 0.22, cy, s * 0.09, 24)]),
            solid(rounded(cx - s * 0.12, cy, s * 0.36, s * 0.3, s * 0.05, angle), s * 0.04, c, z0=s * 0.07)]


def p_wheel(cx, cy, s, colors, angle=0.0):
    tire = colors[0]
    hub = colors[1] if len(colors) > 1 else shade(colors[0], 1.4)
    return [solid(circle(cx, cy, s * 0.47, 48), s * 0.26, tire, holes=[circle(cx, cy, s * 0.31, 40)]),
            solid(circle(cx, cy, s * 0.31, 40), s * 0.22, hub, holes=[circle(cx, cy, s * 0.07, 20)])]


def p_cable(cx, cy, s, colors, angle=0.0):
    c = colors[0]
    return [solid(rect(cx, cy + s * 0.32, s * 0.8, s * 0.16, angle), s * 0.05, c),
            solid(arc_band(cx, cy - s * 0.04, s * 0.36, s * 0.22, 320 + angle, 580 + angle), s * 0.22, c)]


def p_battery(cx, cy, s, colors, angle=0.0):
    c = colors[0]
    w, d = s * 0.98, s * 0.72
    holes = [circle(cx - w * 0.36 + i * w * 0.24, cy - d * 0.28 + j * d * 0.28, s * 0.075, 24)
             for i in range(4) for j in range(3)]
    return [solid(rounded(cx, cy, w, d, s * 0.06, angle), s * 0.36, c, holes=holes)]


def p_battery_lid(cx, cy, s, colors, angle=0.0):
    c = colors[1] if len(colors) > 1 else colors[0]
    return [solid(rounded(cx, cy, s * 0.98, s * 0.72, s * 0.06, angle), s * 0.06, c),
            solid(rounded(cx, cy, s * 0.4, s * 0.12, s * 0.05, angle), s * 0.05, shade(c, 0.85), z0=s * 0.06)]


def p_bushing(cx, cy, s, colors, angle=0.0):
    return [solid(circle(cx, cy, s * 0.4, 36), s * 0.42, colors[0], holes=[circle(cx, cy, s * 0.22, 30)])]


def p_organizer(cx, cy, s, colors, angle=0.0):
    c = colors[0]
    w, d = s * 1.0, s * 0.72
    holes = [rect(cx - w * 0.31, cy - d * 0.2, w * 0.3, d * 0.48), rect(cx, cy - d * 0.2, w * 0.24, d * 0.48),
             rect(cx + w * 0.3, cy - d * 0.2, w * 0.3, d * 0.48), rect(cx - w * 0.16, cy + d * 0.26, w * 0.6, d * 0.32),
             rect(cx + w * 0.3, cy + d * 0.26, w * 0.3, d * 0.32)]
    return [solid(rounded(cx, cy, w, d, s * 0.04), s * 0.28, c, holes=holes)]


def p_insert(cx, cy, s, colors, angle=0.0, which=1):
    c = colors[which] if len(colors) > which else colors[-1]
    return [solid(rounded(cx, cy, s * 0.8, s * 0.56, s * 0.08, angle), s * 0.3, c,
                  holes=[rounded(cx, cy, s * 0.66, s * 0.42, s * 0.05, angle)])]


def p_vent(cx, cy, s, colors, angle=0.0):
    c = colors[0]
    w, d = s * 1.0, s * 0.66
    slots = [rounded(cx, cy - d * 0.32 + i * d * 0.128, w * 0.78, d * 0.06, d * 0.03) for i in range(6)]
    return [solid(rounded(cx, cy, w, d, s * 0.05), s * 0.06, c, holes=slots),
            solid(rounded(cx, cy, w, d, s * 0.05), s * 0.03, shade(c, 0.95), z0=s * 0.06,
                  holes=[rounded(cx, cy, w * 0.9, d * 0.86, s * 0.04)])]


def p_stand(cx, cy, s, colors, angle=0.0):
    a = colors[0]
    b = colors[1] if len(colors) > 1 else shade(colors[0], 1.25)
    return [solid(rounded(cx, cy, s * 0.78, s * 0.7, s * 0.05), s * 0.06, a),
            solid(rect(cx, cy - s * 0.26, s * 0.78, s * 0.14), s * 0.62, b, z0=s * 0.06),
            solid(rect(cx, cy + s * 0.3, s * 0.78, s * 0.07), s * 0.12, a, z0=s * 0.06)]


def p_cover(cx, cy, s, colors, angle=0.0):
    c = colors[0]
    w, d = s * 0.96, s * 0.66
    out = [solid(rounded(cx, cy, w, d, s * 0.08), s * 0.07, c)]
    for qx in (-1, 1):
        for qy in (-1, 1):
            out.append(solid(circle(cx + qx * w * 0.38, cy + qy * d * 0.34, s * 0.06, 20), s * 0.12, shade(c, 1.08),
                             z0=s * 0.07, holes=[circle(cx + qx * w * 0.38, cy + qy * d * 0.34, s * 0.025, 12)]))
    return out


def p_handle(cx, cy, s, colors, angle=0.0):
    c = colors[0]
    return [solid(rounded(cx, cy, s * 0.96, s * 0.24, s * 0.12, angle), s * 0.2, c,
                  holes=[circle(*_rot([(-s * 0.36, 0)], cx, cy, angle)[0], s * 0.04, 14),
                         circle(*_rot([(s * 0.36, 0)], cx, cy, angle)[0], s * 0.04, 14)])]


def p_plug(cx, cy, s, colors, angle=0.0):
    c = colors[0]
    return [solid(circle(cx, cy, s * 0.46, 36), s * 0.1, shade(c, 1.1)),
            solid(circle(cx, cy, s * 0.33, 32), s * 0.34, c, z0=s * 0.1, holes=[circle(cx, cy, s * 0.2, 28)])]


def p_sign(cx, cy, s, colors, angle=0.0):
    a = colors[0]
    b = colors[1] if len(colors) > 1 else shade(colors[0], 0.6)
    w, d = s * 0.98, s * 0.46
    return [solid(rounded(cx, cy, w, d, s * 0.05), s * 0.04, a),
            solid(rect(cx - w * 0.08, cy - d * 0.18, w * 0.62, d * 0.14), s * 0.025, b, z0=s * 0.04),
            solid(rect(cx - w * 0.18, cy + d * 0.06, w * 0.42, d * 0.1), s * 0.025, b, z0=s * 0.04),
            solid(circle(cx + w * 0.34, cy + d * 0.05, d * 0.2, 28), s * 0.025, b, z0=s * 0.04)]


def p_camera(cx, cy, s, colors, angle=0.0):
    c = colors[0]
    return [solid(rounded(cx, cy, s * 0.7, s * 0.5, s * 0.06), s * 0.08, c,
                  holes=[circle(cx - s * 0.25, cy - s * 0.16, s * 0.035, 12), circle(cx + s * 0.25, cy + s * 0.16, s * 0.035, 12)]),
            solid(rounded(cx, cy, s * 0.16, s * 0.16, s * 0.03), s * 0.42, c, z0=s * 0.08),
            solid(circle(cx, cy, s * 0.12, 28), s * 0.12, shade(c, 1.15), z0=s * 0.5)]


def p_hinge(cx, cy, s, colors, angle=0.0):
    c = colors[0]
    out = [solid(rect(cx - s * 0.25, cy, s * 0.42, s * 0.8), s * 0.06, c,
                  holes=[circle(cx - s * 0.28, cy - s * 0.22, s * 0.04, 12), circle(cx - s * 0.28, cy + s * 0.22, s * 0.04, 12)]),
           solid(rect(cx + s * 0.25, cy, s * 0.42, s * 0.8), s * 0.06, c,
                 holes=[circle(cx + s * 0.28, cy - s * 0.22, s * 0.04, 12), circle(cx + s * 0.28, cy + s * 0.22, s * 0.04, 12)])]
    for i in range(3):
        out.append(solid(circle(cx, cy - s * 0.27 + i * s * 0.27, s * 0.08, 20), s * 0.18, shade(c, 1.05)))
    return out


def p_planter(cx, cy, s, colors, angle=0.0):
    c = colors[0]
    return [solid(polygon_n(cx, cy, s * 0.5, 6, math.radians(15)), s * 0.7, c,
                  holes=[polygon_n(cx, cy, s * 0.43, 6, math.radians(15))])]


def p_lamp(cx, cy, s, colors, angle=0.0):
    a = colors[0]
    b = colors[1] if len(colors) > 1 else shade(colors[0], 0.8)
    return [solid(circle(cx, cy, s * 0.48, 56), s * 0.2, a),
            solid(circle(cx, cy, s * 0.36, 48), s * 0.06, b, z0=s * 0.2),
            solid(circle(cx, cy, s * 0.16, 36), s * 0.5, a, z0=s * 0.26, holes=[circle(cx, cy, s * 0.07, 20)])]


SHAPES = {
    "bracket": p_bracket, "gear": p_gear, "case": p_case, "case_lid": p_case_lid, "wheel": p_wheel,
    "cable": p_cable, "battery": p_battery, "battery_lid": p_battery_lid, "bushing": p_bushing,
    "organizer": p_organizer, "insert": p_insert, "insert2": lambda *a, **k: p_insert(*a, which=2, **k),
    "vent": p_vent, "stand": p_stand, "cover": p_cover, "handle": p_handle, "plug": p_plug, "sign": p_sign,
    "camera": p_camera, "hinge": p_hinge, "planter": p_planter, "lamp": p_lamp,
}


def layout(objects, colors, plate=PLATE_MM):
    """objects: [(shape, count)] → [{"index", "shape", "solids", "foot"}], the parts in a grid that
    fills most of the plate (the parts are drawn to fit it, not to a real scale)."""
    shapes = [shape for shape, count in objects for _ in range(count)]
    n = len(shapes)
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = math.ceil(n / cols)
    margin = plate * 0.1
    cell = min((plate - 2 * margin) / cols, (plate - 2 * margin) / rows)
    x0 = (plate - cell * cols) / 2
    y0 = (plate - cell * rows) / 2
    pal = [rgb(c) for c in colors]
    out = []
    for i, shape in enumerate(shapes):
        r, c = divmod(i, cols)
        cx, cy = x0 + cell * (c + 0.5), y0 + cell * (r + 0.5)
        solids = SHAPES[shape](cx, cy, cell * 0.8, pal)
        out.append({"index": i, "shape": shape, "solids": solids})
    return out


# --- drawing -------------------------------------------------------------------------------

def _area(poly):
    return sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1])) / 2


def _ccw(poly):
    return poly if _area(poly) > 0 else list(reversed(poly))


class _View:
    def __init__(self, scale, ox, oy):
        self.scale, self.ox, self.oy = scale, ox, oy

    def __call__(self, x, y, z=0.0):
        return (self.ox + (x - y) * COS30 * self.scale, self.oy + ((x + y) * SIN30 - z) * self.scale)


def _faces(s):
    """The solid's side faces a viewer at +x +y sees, far ones first: (depth, quad points, light)."""
    poly = _ccw(s["poly"])
    z0, z1 = s["z0"], s["z0"] + s["h"]
    out = []
    for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
        nx, ny = by - ay, -(bx - ax)
        length = math.hypot(nx, ny) or 1.0
        nx, ny = nx / length, ny / length
        if nx + ny <= 0:
            continue
        light = 0.58 + 0.3 * (ny - nx + 1) / 2
        out.append(((ax + bx + ay + by) / 2, [(ax, ay, z0), (bx, by, z0), (bx, by, z1), (ax, ay, z1)], light))
    out.sort(key=lambda f: f[0])
    return out


def _depth(part):
    xs = [x for s in part["solids"] for x, _ in s["poly"]]
    ys = [y for s in part["solids"] for _, y in s["poly"]]
    return (min(xs) + max(xs) + min(ys) + max(ys)) / 2


def render(parts, width=640, plate=PLATE_MM, with_plate=True, ss=3):
    """The plate preview: PNG bytes, transparent around the plate."""
    top_z = max((s["z0"] + s["h"] for p in parts for s in p["solids"]), default=0.0)
    slab = 7.0
    pad = 0.05 * plate
    xs = [(-pad - (plate + pad)) * COS30, (plate + pad + pad) * COS30]
    span_x = xs[1] - xs[0]
    span_y = (2 * (plate + pad) * SIN30 + slab + top_z + pad * 2)
    W = width * ss
    scale = W / span_x
    H = int(math.ceil(span_y * scale))
    view = _View(scale, -xs[0] * scale, (top_z + pad + pad * SIN30) * scale)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if with_plate:
        top = (86, 92, 103)
        p = [(0, 0), (plate, 0), (plate, plate), (0, plate)]
        draw.polygon([view(x, y, -slab) for x, y in [(plate, 0), (plate, plate)]] +
                     [view(x, y, 0) for x, y in [(plate, plate), (plate, 0)]], fill=(50, 54, 62))
        draw.polygon([view(x, y, -slab) for x, y in [(plate, plate), (0, plate)]] +
                     [view(x, y, 0) for x, y in [(0, plate), (plate, plate)]], fill=(64, 69, 78))
        draw.polygon([view(x, y) for x, y in p], fill=top)
        inset = plate * 0.035
        draw.polygon([view(x, y) for x, y in [(inset, inset), (plate - inset, inset), (plate - inset, plate - inset),
                                              (inset, plate - inset)]], outline=(108, 114, 126), width=max(1, ss))
        for i in range(1, 8):
            t = plate * i / 8
            draw.line([view(t, inset), view(t, plate - inset)], fill=(95, 101, 113), width=max(1, ss // 2))
            draw.line([view(inset, t), view(plate - inset, t)], fill=(95, 101, 113), width=max(1, ss // 2))
        # soft shadows under the parts
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        for part in parts:
            for s in part["solids"]:
                if s["z0"] == 0:
                    sd.polygon([view(x + 2.5, y + 2.5) for x, y in s["poly"]], fill=(0, 0, 0, 120))
        shadow = shadow.filter(ImageFilter.GaussianBlur(3 * ss))
        img.alpha_composite(shadow)
        draw = ImageDraw.Draw(img)
    for part in sorted(parts, key=_depth):
        for s in sorted(part["solids"], key=lambda s: s["z0"]):
            base = s["color"]
            for _, quad, light in _faces(s):
                draw.polygon([view(*q) for q in quad], fill=shade(base, light))
            z1 = s["z0"] + s["h"]
            top_poly = [view(x, y, z1) for x, y in s["poly"]]
            draw.polygon(top_poly, fill=lift(base, 0.12), outline=lift(base, 0.32), width=max(1, ss // 2))
            for hole in s["holes"]:
                draw.polygon([view(x, y, z1) for x, y in hole], fill=shade(base, 0.38), outline=shade(base, 0.6))
    img = img.resize((width, max(1, H // ss)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_pick(parts, ids, plate=PLATE_MM, size=480):
    """The plate map (Bambu Studio's pick_N.png): a top view, each part filled with its id as a colour
    (red = low byte, green, blue), no smoothing; transparent elsewhere."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    k = size / plate
    for part, pid in zip(parts, ids):
        color = (pid & 0xFF, (pid >> 8) & 0xFF, (pid >> 16) & 0xFF, 255)
        for s in part["solids"]:
            draw.polygon([(x * k, y * k) for x, y in s["poly"]], fill=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
