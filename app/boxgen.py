"""Basic box generator: finger-jointed panels, split into puzzle-jointed tiles when they exceed the sheet.

The box is described by its outer size. Panels are built as rectangles minus notches; every notch of one
panel is a tab of its neighbour, so the finger joints interlock. A panel larger than the sheet is cut into
a grid of tiles whose neighbours join with a dovetail ("jigsaw") knob.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box as rect
from shapely.ops import unary_union

from .parser import Part

MAX_TILES_PER_PANEL = 100
MIN_TILE_AREA = 4.0          # mm2: smaller fragments left over by notches are dropped


class BoxError(Exception):
    """Carries a machine-readable code, like ParseError."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class BoxParams:
    width: float            # outer size, x
    depth: float            # outer size, y
    height: float           # outer size, z
    thickness: float        # material thickness
    finger: float = 12.0    # target finger width
    kerf: float = 0.0       # laser kerf, compensates the fit of every joint
    lid: bool = True        # closed top; otherwise the top is open
    split: bool = True      # cut panels that exceed max_w x max_h into puzzle-jointed tiles
    max_w: float = 390.0
    max_h: float = 390.0
    knob: float = 6.0       # radius of the puzzle knob head


@dataclass
class Panel:
    name: str
    quantity: int
    polygon: Polygon


def _segments(start: float, end: float, finger: float) -> list[tuple[float, float]]:
    """Equal segments between start and end, an odd number so both ends belong to the same side."""
    n = max(3, round((end - start) / finger))
    if n % 2 == 0:
        n += 1
    step = (end - start) / n
    return [(start + i * step, start + (i + 1) * step) for i in range(n)]


def _panel(a: float, b: float, t: float, finger: float, edges: dict[str, tuple[float, float, bool] | None]) -> Polygon:
    """Rectangle a x b minus notches of depth t on the given edges.

    An edge is (span start, span end, owns first): along the span the edge alternates between a tab of this
    panel and a notch for the neighbour's tab; the panel owns the even segments when "owns first" is set.
    """
    poly = rect(0, 0, a, b)
    cuts = []
    for edge, spec in edges.items():
        if spec is None:
            continue
        start, end, owns_first = spec
        for i, (s0, s1) in enumerate(_segments(start, end, finger)):
            if (i % 2 == 0) == owns_first:
                continue  # a tab of this panel
            if edge == "bottom":
                cuts.append(rect(s0, 0, s1, t))
            elif edge == "top":
                cuts.append(rect(s0, b - t, s1, b))
            elif edge == "left":
                cuts.append(rect(0, s0, t, s1))
            elif edge == "right":
                cuts.append(rect(a - t, s0, a, s1))
    return poly.difference(unary_union(cuts)) if cuts else poly


def _knob(cx: float, cy: float, r: float, direction: tuple[float, float]) -> Polygon:
    """Dovetail knob on a cut line through (cx, cy): neck and round head, growing along `direction`."""
    neck = rect(-0.45 * r, -0.2 * r, 0.45 * r, 1.4 * r)
    head = Point(0, 1.6 * r).buffer(r, quad_segs=16)
    shape = unary_union([neck, head])
    angle = math.degrees(math.atan2(direction[1], direction[0])) - 90
    return affinity.translate(affinity.rotate(shape, angle, origin=(0, 0)), cx, cy)


def _knob_depth(r: float) -> float:
    return 2.6 * r


def _fit_orientation(w: float, h: float, mw: float, mh: float, r: float) -> tuple[float, float, int, int]:
    """Choose how a w x h panel is gridded so every tile (with its knobs) fits mw x mh, rotated or not."""
    best = None
    for tw, th in ((mw, mh), (mh, mw)):
        d = _knob_depth(r)
        cols = 1 if w <= tw else math.ceil((w - d) / (tw - 2 * d))
        rows = 1 if h <= th else math.ceil((h - d) / (th - 2 * d))
        if best is None or cols * rows < best[2] * best[3]:
            best = (tw, th, cols, rows)
    return best


def _split(poly: Polygon, p: BoxParams) -> list[Polygon]:
    minx, miny, maxx, maxy = poly.bounds
    w, h = maxx - minx, maxy - miny
    mw, mh = p.max_w, p.max_h
    if (w <= mw and h <= mh) or (w <= mh and h <= mw):
        return [poly]
    r = p.knob
    if mw < 10 * r or mh < 10 * r:
        r = max(1.0, min(mw, mh) / 10)
    _, _, cols, rows = _fit_orientation(w, h, mw, mh, r)
    if cols * rows > MAX_TILES_PER_PANEL:
        raise BoxError("box_too_many_tiles", str(cols * rows))
    col_w, row_h = w / cols, h / rows
    r = min(r, col_w / 12, row_h / 12)
    pad = 10 * r + 1

    def region(cuts: list[float], vertical: bool, seg: float, lo: float):
        """Regions between consecutive cut lines; knobs sit in the middle of each segment of the other axis."""
        a_list = []
        for k, pos in enumerate(cuts):
            if vertical:
                side = rect(minx - pad, miny - pad, pos, maxy + pad)
            else:
                side = rect(minx - pad, miny - pad, maxx + pad, pos)
            add, sub = [], []
            count = rows if vertical else cols
            for j in range(count):
                c = lo + (j + 0.5) * seg
                grow_right = (j + k) % 2 == 0
                if vertical:
                    shape = _knob(pos, c, r, (1, 0) if grow_right else (-1, 0))
                else:
                    shape = _knob(c, pos, r, (0, 1) if grow_right else (0, -1))
                (add if grow_right else sub).append(shape)
            a_list.append(unary_union([side, *add]).difference(unary_union(sub)) if sub else unary_union([side, *add]))
        everything = rect(minx - pad, miny - pad, maxx + pad, maxy + pad)
        out, prev = [], None
        for a in [*a_list, everything]:
            out.append(a if prev is None else a.difference(prev))
            prev = a
        return out

    col_cuts = [minx + i * col_w for i in range(1, cols)]
    row_cuts = [miny + j * row_h for j in range(1, rows)]
    col_regions = region(col_cuts, True, row_h, miny)
    row_regions = region(row_cuts, False, col_w, minx)
    tiles = []
    for i, cr in enumerate(col_regions):
        for j, rr in enumerate(row_regions):
            cell = poly.intersection(cr).intersection(rr)
            for g in getattr(cell, "geoms", [cell]):
                if isinstance(g, Polygon) and g.area >= MIN_TILE_AREA:
                    tiles.append((i, j, g))
    tiles.sort(key=lambda x: (x[1], x[0]))
    return [g for _, _, g in tiles]


def _validate(p: BoxParams) -> None:
    t = p.thickness
    if not (0.1 <= t <= 50):
        raise BoxError("box_dims", "thickness")
    if min(p.width, p.depth, p.height) < 6 * t:
        raise BoxError("box_dims", "size")
    if max(p.width, p.depth, p.height) > 5000:
        raise BoxError("box_dims", "size")
    if p.finger < 1.5 * t:
        raise BoxError("box_finger", str(round(1.5 * t, 1)))
    if not (0 <= p.kerf <= t):
        raise BoxError("box_dims", "kerf")
    if p.split and (p.max_w < 40 or p.max_h < 40):
        raise BoxError("box_dims", "sheet")


def box_panels(p: BoxParams) -> list[Panel]:
    """The distinct panels of the box with their quantities (front = back, sides, base = lid)."""
    _validate(p)
    t, f = p.thickness, p.finger
    w, d, h = p.width, p.depth, p.height
    z1 = h - t if p.lid else h          # vertical finger joints stop at the lid, or run to the open top

    def horizontal(length: float, owns_first: bool):
        return (0, length, owns_first)

    front = _panel(w, h, t, f, {
        "bottom": horizontal(w, False),
        "top": horizontal(w, False) if p.lid else None,
        "left": (t, z1, True),
        "right": (t, z1, True),
    })
    side = _panel(d, h, t, f, {
        "bottom": horizontal(d, False),
        "top": horizontal(d, False) if p.lid else None,
        "left": (t, z1, False),
        "right": (t, z1, False),
    })
    base = _panel(w, d, t, f, {
        "bottom": horizontal(w, True), "top": horizontal(w, True),
        "left": horizontal(d, True), "right": horizontal(d, True),
    })
    return [Panel("front", 2, front), Panel("side", 2, side), Panel("base", 2 if p.lid else 1, base)]


def box_parts(p: BoxParams, file_id: str = "") -> list[tuple[Part, int]]:
    """Parts ready for nesting with their quantities."""
    out: list[tuple[Part, int]] = []
    for panel in box_panels(p):
        tiles = _split(panel.polygon, p) if p.split else [panel.polygon]
        for k, tile in enumerate(tiles, 1):
            if p.kerf:
                tile = tile.buffer(p.kerf / 2, join_style=2, mitre_limit=2.0)
            minx, miny, _, _ = tile.bounds
            tile = affinity.translate(tile, -minx, -miny)
            name = f"box {panel.name}" if len(tiles) == 1 else f"box {panel.name} {k}/{len(tiles)}"
            rings = [LineString(tile.exterior.coords), *(LineString(i.coords) for i in tile.interiors)]
            out.append((Part(id=uuid.uuid4().hex[:8], name=name, source=f"box {p.width:g}x{p.depth:g}x{p.height:g}",
                             polygon=tile, cut=rings, file_id=file_id), panel.quantity))
    return out
