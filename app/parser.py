"""Loading SVG/DXF files into shapely parts and exporting nested sheets back to SVG/DXF."""
from __future__ import annotations

import colorsys
import hashlib
import io
import os
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator
from xml.sax.saxutils import escape

import ezdxf
import numpy as np
from ezdxf import colors as dxf_colors
from ezdxf import path as ezpath
from ezdxf import recover
from shapely import affinity, make_valid, set_precision
from shapely.geometry import LineString, MultiPolygon, Polygon
from scipy.spatial import cKDTree
from shapely import STRtree
from shapely.ops import polygonize, unary_union
from svgelements import SVG, Close, Move, Path, Shape
from svgelements import Linear

PX_TO_MM = 25.4 / 96.0
MIN_PART_AREA = 0.25
CLOSE_TOL = 0.05
SNAP_GRID = 0.01
ENDPOINT_SNAP = 0.1          # gap (mm) in an outline that is still treated as closed
CURVE_STEP_MM = 0.5
DXF_FLATTEN_MM = 0.03
SIMPLIFY_MM = 0.003

DXF_UNIT_TO_MM = {
    1: 25.4, 2: 304.8, 3: 1609344.0, 4: 1.0, 5: 10.0, 6: 1000.0, 7: 1e6,
    8: 2.54e-5, 9: 0.0254, 10: 914.4, 13: 0.001, 14: 100.0,
}
DXF_SUPPORTED = {"LWPOLYLINE", "POLYLINE", "LINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"}


class ParseError(Exception):
    """Carries a machine-readable code so the UI can show the message in its own language."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class Part:
    id: str
    name: str
    source: str
    polygon: Polygon | MultiPolygon          # material outline used for nesting
    cut: list[LineString] = field(default_factory=list)       # every cut line (outline, holes, slits)
    engrave: list[LineString] = field(default_factory=list)   # engraved lines
    fills: list[Polygon | MultiPolygon] = field(default_factory=list)  # engraved areas (hatch)
    file_id: str = ""

    @property
    def width(self) -> float:
        return self.polygon.bounds[2]

    @property
    def height(self) -> float:
        return self.polygon.bounds[3]

    @property
    def area(self) -> float:
        return self.polygon.area


# --------------------------------------------------------------------------- #
# Ring classification and part assembly
# --------------------------------------------------------------------------- #

ROLE_CUT = "cut"
ROLE_ENGRAVE = "engrave"
ROLE_IGNORE = "ignore"
ORPHAN_DISTANCE = 2.0
ENGRAVE_WORDS = ("grav", "engrav", "score", "etch", "ryt", "mark")
CUT_WORDS = ("cut", "rez", "řez", "řež")


@dataclass
class Linework:
    pts: list[tuple[float, float]]
    closed: bool
    role: str
    style: tuple[str, str, str, str] = ("", "", "", "")   # (key, colour name, #rrggbb, layer) for per-file role mapping
    group: int = 0   # hatch rings of one HATCH entity share a group (even-odd is applied per entity)


@dataclass
class Parsed:
    lines: list[Linework]
    hatch: list[Linework]

    def styles(self) -> list[dict]:
        """Distinct pens/layers found in the file with their default role."""
        found: dict[str, dict] = {}
        for kind, items in (("line", self.lines), ("hatch", self.hatch)):
            for l in items:
                key, cname, color, layer = l.style
                if l.role == ROLE_IGNORE and _is_white(_rgb(color)):
                    continue  # white is always ignored and never offered
                entry = found.setdefault(key, {"key": key, "color_name": cname, "color": color, "layer": layer,
                                               "kind": kind, "count": 0, "role": l.role})
                entry["count"] += 1
        return sorted(found.values(), key=lambda e: -e["count"])


def _is_red(rgb) -> bool:
    r, g, b = rgb[0], rgb[1], rgb[2]
    return r >= 180 and g <= 100 and b <= 100


def _is_white(rgb) -> bool:
    return min(rgb[0], rgb[1], rgb[2]) >= 240


def color_name(rgb) -> str:
    """Key of the nearest basic colour name (translated by the UI)."""
    h, sat, val = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    if val < 0.25:
        return "black"
    if sat < 0.15:
        return "white" if val > 0.9 else "gray"
    deg = h * 360
    if 10 <= deg < 40 and val < 0.6:
        return "brown"
    for limit, name in ((15, "red"), (40, "orange"), (70, "yellow"), (160, "green"), (200, "cyan"),
                        (255, "blue"), (290, "violet"), (335, "magenta")):
        if deg < limit:
            return name
    return "red"


def _is_dark(rgb) -> bool:
    return max(rgb[0], rgb[1], rgb[2]) < 70


def _to_polygons(pts: list[tuple[float, float]]) -> list[Polygon]:
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = make_valid(poly)
    if poly.is_empty:
        return []
    rings: list[Polygon] = []
    for g in getattr(poly, "geoms", [poly]):
        if not isinstance(g, Polygon):
            continue
        # a repaired self-touching ring can come back with holes: keep them as rings of their own,
        # the even-odd step rebuilds the nesting
        for ring in [g.exterior, *g.interiors]:
            candidate = Polygon(ring)
            if candidate.area >= MIN_PART_AREA:
                rings.append(candidate)
    return rings


def _even_odd(rings: Iterable[Polygon]) -> list[tuple]:
    """Group rings into trees; the material of a tree is the even-odd combination of its rings.

    Returns (material geometry, outline polygon) per top-level ring.
    """
    unique: dict[tuple, Polygon] = {}
    for ring in rings:
        c = ring.centroid
        unique.setdefault((round(ring.area, 2), round(c.x, 1), round(c.y, 1)), ring)
    ordered = sorted(unique.values(), key=lambda r: -r.area)
    points = [r.representative_point() for r in ordered]

    root: list[int] = []
    for i, ring in enumerate(ordered):
        owner = i
        for j in range(i - 1, -1, -1):
            if ordered[j].area > ring.area and ordered[j].contains(points[i]):
                owner = root[j]
                break
        root.append(owner)

    result = []
    for i, ring in enumerate(ordered):
        if root[i] != i:
            continue
        geom = Polygon(ring.exterior)
        for k in range(i + 1, len(ordered)):
            if root[k] == i:
                geom = geom.symmetric_difference(Polygon(ordered[k].exterior))
        result.append((geom, Polygon(ring.exterior)))
    return result


def _collect(pts: list[tuple[float, float]], role: str, style, out: list[Linework]) -> None:
    if len(pts) < 2:
        return
    gap = np.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1])
    if gap < CLOSE_TOL and len(pts) >= 4:
        out.append(Linework(pts[:-1], True, role, style))
    else:
        out.append(Linework(pts, False, role, style))


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _line_string(item: Linework) -> LineString:
    pts = item.pts + [item.pts[0]] if item.closed else item.pts
    ls = LineString(pts)
    return ls.simplify(SIMPLIFY_MM) if len(pts) > 2 else ls


def _snap_endpoints(lines: list[list[tuple[float, float]]], tol: float = ENDPOINT_SNAP) -> list[list[tuple[float, float]]]:
    """Merge line endpoints that lie within tol of each other so almost-touching lines form closed outlines."""
    ends = np.array([pt for ln in lines for pt in (ln[0], ln[-1])], dtype=float)
    if len(ends) < 2:
        return lines
    parent = list(range(len(ends)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in cKDTree(ends).query_pairs(tol):
        parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(ends)):
        groups.setdefault(find(i), []).append(i)
    centre = {i: ends[idx].mean(axis=0) for idx in groups.values() for i in idx}
    out = []
    for k, ln in enumerate(lines):
        ln = list(ln)
        ln[0] = tuple(centre[2 * k])
        ln[-1] = tuple(centre[2 * k + 1])
        out.append(ln)
    return out


def _apply_roles(items: list[Linework], overrides: dict[str, str]) -> list[Linework]:
    result = []
    for l in items:
        role = overrides.get(l.style[0], l.role)
        if role != ROLE_IGNORE:
            result.append(replace(l, role=role))
    return result


def _assemble(parsed: Parsed, overrides: dict[str, str]) -> tuple[list[dict], int]:
    """Turn classified linework into pieces: material outline plus every cut/engrave line belonging to it."""
    lines = _apply_roles(parsed.lines, overrides)
    hatch_groups: dict[int, list[list[tuple[float, float]]]] = {}
    for l in _apply_roles(parsed.hatch, overrides):
        if l.role == ROLE_ENGRAVE:
            hatch_groups.setdefault(l.group, []).append(l.pts)
    if not overrides and not any(l.role == ROLE_CUT for l in lines):
        lines = [replace(l, role=ROLE_CUT) for l in lines]  # nothing marked as cut: treat everything as cut

    rings: list[Polygon] = []
    open_cut: list[LineString] = []
    for l in lines:
        if l.role != ROLE_CUT:
            continue
        if l.closed and len(l.pts) >= 3:
            rings.extend(_to_polygons(l.pts))
        elif len(l.pts) >= 2:
            open_cut.append(LineString(l.pts))
    if open_cut:
        open_cut = [LineString(pts) for pts in _snap_endpoints([list(l.coords) for l in open_cut])]
        noded = set_precision(unary_union(open_cut), SNAP_GRID)
        rings.extend(f for f in polygonize(noded) if f.area >= MIN_PART_AREA)

    pieces = _even_odd(rings)
    if not pieces:
        return [], 0
    hulls = [hull.buffer(0.05) for _, hull in pieces]
    tree = STRtree(hulls)
    areas = [h.area for h in hulls]

    def owner(point) -> int | None:
        hits = tree.query(point, predicate="intersects")
        if len(hits):
            return int(min(hits, key=lambda i: areas[i]))
        j = int(tree.nearest(point))
        return j if hulls[j].distance(point) <= ORPHAN_DISTANCE else None

    out = [{"polygon": geom, "cut": [], "engrave": [], "fills": []} for geom, _ in pieces]
    orphans = 0
    for l in lines:
        ls = _line_string(l)
        if ls.is_empty or ls.length == 0:
            continue
        idx = owner(ls.interpolate(0.5, normalized=True))
        if idx is None:
            orphans += 1
        else:
            out[idx]["cut" if l.role == ROLE_CUT else "engrave"].append(ls)

    for group in hatch_groups.values():
        hatch_polys = [poly for pts in group for poly in _to_polygons(pts)]
        for geom, _ in _even_odd(hatch_polys):
            idx = owner(geom.representative_point())
            if idx is None:
                orphans += 1
            else:
                out[idx]["fills"].append(geom)
    return out, orphans


# --------------------------------------------------------------------------- #
# SVG
# --------------------------------------------------------------------------- #

def _flatten_svg_subpath(sub) -> tuple[list[tuple[float, float]], bool]:
    pts: list[tuple[float, float]] = []
    closed = False

    def add(p) -> None:
        if p is not None and p.x is not None and p.y is not None and np.isfinite(p.x) and np.isfinite(p.y):
            pts.append((p.x * PX_TO_MM, p.y * PX_TO_MM))

    for seg in sub:
        if isinstance(seg, Move):
            add(seg.end)
        elif isinstance(seg, Close):
            closed = True
            add(seg.end)
        elif isinstance(seg, Linear):
            if not pts:
                add(seg.start)
            add(seg.end)
        else:
            if not pts:
                add(seg.start)
            length_mm = seg.length(error=1e-3) * PX_TO_MM
            steps = int(min(400, max(8, np.ceil(length_mm / CURVE_STEP_MM))))
            for t in np.linspace(0.0, 1.0, steps + 1)[1:]:
                add(seg.point(float(t)))
    return pts, closed


def _svg_style(el) -> tuple[str, tuple[str, str, str, str], bool, bool]:
    """(role, style, has stroke, has fill); the pen colour is the stroke, or the fill of fill-only shapes."""
    values = getattr(el, "values", None) or {}
    stroke = getattr(el, "stroke", None)
    has_stroke = stroke is not None and getattr(stroke, "value", None) is not None
    fill = getattr(el, "fill", None)
    has_fill = "fill" in values and fill is not None and getattr(fill, "value", None) is not None
    pen = stroke if has_stroke else (fill if has_fill else None)
    if pen is None:
        return ROLE_CUT, ("none", "none", "#000000", ""), has_stroke, has_fill
    rgb = (pen.red, pen.green, pen.blue)
    color = _hex(rgb)
    if _is_white(rgb):
        role = ROLE_IGNORE
    else:
        role = ROLE_CUT if _is_red(rgb) or _is_dark(rgb) else ROLE_ENGRAVE
    return role, (color, color_name(rgb), color, ""), has_stroke, has_fill


def parse_svg(data: bytes) -> Parsed:
    try:
        svg = SVG.parse(io.BytesIO(data), reify=True, ppi=96.0)
    except Exception as exc:
        raise ParseError("invalid_svg", str(exc)) from exc

    lines: list[Linework] = []
    hatch: list[Linework] = []
    fill_only: list[tuple[list[tuple[float, float]], bool, str, tuple, int]] = []
    group = 0
    for el in svg.elements():
        if not isinstance(el, Shape):
            continue
        values = getattr(el, "values", None) or {}
        if str(values.get("display", "")).strip() == "none" or str(values.get("visibility", "")).strip() == "hidden":
            continue
        role, style, has_stroke, has_fill = _svg_style(el)
        group += 1
        path = abs(el if isinstance(el, Path) else Path(el))
        for sub in path.as_subpaths():
            pts, is_closed = _flatten_svg_subpath(sub)
            closed = is_closed and len(pts) >= 3
            if closed and np.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < CLOSE_TOL:
                pts = pts[:-1]
            if has_fill and not has_stroke and closed:
                fill_only.append((pts, closed, role, style, group))      # decided below, needs the whole file
                continue
            if closed:
                if role == ROLE_ENGRAVE and has_fill:
                    hatch.append(Linework(pts, True, role, style, group))
                lines.append(Linework(pts, True, role, style))
            else:
                _collect(pts, role, style, lines)

    # Filled shapes without a stroke: in a file that also has stroked cut lines they are artwork to engrave
    # (an engraved area), in a file made only of fills they are the silhouettes to cut.
    artwork = any(l.role == ROLE_CUT for l in lines)
    for pts, closed, role, style, grp in fill_only:
        if artwork:
            key, cname, color, layer = style
            hatch.append(Linework(pts, True, ROLE_IGNORE if role == ROLE_IGNORE else ROLE_ENGRAVE,
                                  (f"fill|{key}", cname, color, layer), grp))
        else:
            lines.append(Linework(pts, True, role, style))
    return Parsed(lines, hatch)


# --------------------------------------------------------------------------- #
# DXF
# --------------------------------------------------------------------------- #

def _entity_rgb(e, doc, inherited):
    true_color = e.dxf.get("true_color")
    if true_color is not None:
        return dxf_colors.int2rgb(true_color)
    aci = e.dxf.get("color", 256)
    if aci == 0 and inherited is not None:
        return inherited
    if 1 <= aci <= 255:
        return dxf_colors.aci2rgb(aci)
    layer = e.dxf.get("layer", "0")
    if doc.layers.has_entry(layer):
        entry = doc.layers.get(layer)
        if entry.dxf.get("true_color") is not None:
            return dxf_colors.int2rgb(entry.dxf.true_color)
        return dxf_colors.aci2rgb(min(255, max(1, abs(entry.dxf.get("color", 7)))))
    return (255, 255, 255)


def _dxf_style(e, doc, inherited) -> tuple[str, tuple[str, str, str]]:
    layer = e.dxf.get("layer", "0")
    rgb = _entity_rgb(e, doc, inherited)
    color = _hex(rgb)
    cname = color_name(rgb)
    if e.dxftype() == "HATCH":
        return (ROLE_IGNORE if _is_white(rgb) else ROLE_ENGRAVE), (
            f"{layer}|hatch|{color}", cname, color, layer)
    name = layer.lower()
    if _is_white(rgb):
        role = ROLE_IGNORE  # white = areas left after engraving, they drop out
    elif any(w in name for w in ENGRAVE_WORDS):
        role = ROLE_ENGRAVE
    elif any(w in name for w in CUT_WORDS):
        role = ROLE_CUT
    else:
        role = ROLE_CUT if _is_red(rgb) else ROLE_ENGRAVE
    return role, (f"{layer}|{color}", cname, color, layer)


def _dxf_entities(entities, doc, inherited=None, depth: int = 0) -> Iterator:
    for e in entities:
        kind = e.dxftype()
        layer = e.dxf.get("layer", "0")
        if doc.layers.has_entry(layer):
            entry = doc.layers.get(layer)
            if entry.is_off() or entry.is_frozen():
                continue
        if kind == "INSERT":
            if depth < 8:
                own = e.dxf.get("true_color")
                color = dxf_colors.int2rgb(own) if own is not None else (
                    dxf_colors.aci2rgb(e.dxf.color) if 1 <= e.dxf.get("color", 256) <= 255 else inherited)
                yield from _dxf_entities(e.virtual_entities(), doc, color, depth + 1)
        elif kind in DXF_SUPPORTED or kind == "HATCH":
            yield e, inherited


def parse_dxf(data: bytes) -> Parsed:
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
        tmp.write(data)
        name = tmp.name
    try:
        try:
            doc = ezdxf.readfile(name)
        except (ezdxf.DXFError, UnicodeDecodeError):
            doc, _ = recover.readfile(name)
    except Exception as exc:
        raise ParseError("invalid_dxf", str(exc)) from exc
    finally:
        os.unlink(name)

    scale = DXF_UNIT_TO_MM.get(int(doc.header.get("$INSUNITS", 0) or 0), 1.0)
    distance = DXF_FLATTEN_MM / scale
    lines: list[Linework] = []
    hatch: list[Linework] = []
    group = 0
    for e, inherited in _dxf_entities(doc.modelspace(), doc):
        role, style = _dxf_style(e, doc, inherited)
        try:
            if e.dxftype() == "HATCH":
                group += 1
                for p in ezpath.from_hatch(e):
                    for sub in p.sub_paths():
                        pts = [(v.x * scale, -v.y * scale) for v in sub.flattening(distance)]
                        if len(pts) >= 4 and np.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < CLOSE_TOL:
                            pts = pts[:-1]
                        if len(pts) >= 3:
                            hatch.append(Linework(pts, True, ROLE_ENGRAVE, style, group))
                continue
            p = ezpath.make_path(e)
        except (TypeError, ValueError, ezdxf.DXFError):
            continue
        for sub in p.sub_paths():
            pts = [(v.x * scale, -v.y * scale) for v in sub.flattening(distance)]
            _collect(pts, role, style, lines)
    return Parsed(lines, hatch)


# --------------------------------------------------------------------------- #
# Public loading API
# --------------------------------------------------------------------------- #

def parse_file(filename: str, data: bytes) -> Parsed:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".svg":
        return parse_svg(data)
    if ext == ".dxf":
        return parse_dxf(data)
    raise ParseError("unsupported")


def build_parts(filename: str, parsed: Parsed, overrides: dict[str, str] | None = None) -> tuple[list[Part], list[dict]]:
    pieces, orphans = _assemble(parsed, overrides or {})
    if not pieces:
        raise ParseError("no_shape")

    stem = os.path.splitext(os.path.basename(filename))[0]
    parts = []
    for k, piece in enumerate(pieces, 1):
        minx, miny, _, _ = piece["polygon"].bounds

        def shift(g):
            return affinity.translate(g, -minx, -miny)

        name = stem if len(pieces) == 1 else f"{stem}#{k}"
        parts.append(Part(
            id=uuid.uuid4().hex[:8], name=name, source=filename,
            polygon=shift(piece["polygon"]),
            cut=[shift(g) for g in piece["cut"]],
            engrave=[shift(g) for g in piece["engrave"]],
            fills=[shift(g) for g in piece["fills"]],
        ))
    warnings = [{"code": "orphans", "file": filename, "count": orphans}] if orphans else []
    return parts, warnings


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def transform_polygon(geom, angle: float, tx: float, ty: float):
    """Rotate about the origin of the part's own frame, then translate."""
    return affinity.translate(affinity.rotate(geom, angle, origin=(0, 0)), tx, ty)


@dataclass
class PlacedPart:
    name: str
    outline: Polygon | MultiPolygon
    cut: list[LineString]
    engrave: list[LineString]
    fills: list


def place_part(part: Part, angle: float, tx: float, ty: float, name: str | None = None) -> PlacedPart:
    def move(g):
        return transform_polygon(g, angle, tx, ty)

    return PlacedPart(name or part.name, move(part.polygon), [move(g) for g in part.cut],
                      [move(g) for g in part.engrave], [move(g) for g in part.fills])


def _polygons(geom) -> list[Polygon]:
    return list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]


def _fmt(coords, precision: int = 3) -> str:
    return " L".join(f"{x:.{precision}f},{y:.{precision}f}" for x, y in coords)


def polygon_path_d(geom, precision: int = 3) -> str:
    parts = []
    for poly in _polygons(geom):
        for ring in [poly.exterior, *poly.interiors]:
            parts.append(f"M{_fmt(list(ring.coords)[:-1], precision)} Z")
    return " ".join(parts)


def lines_path_d(lines: list[LineString], precision: int = 3) -> str:
    parts = []
    for ls in lines:
        coords = list(ls.coords)
        if len(coords) > 3 and ls.is_closed:
            parts.append(f"M{_fmt(coords[:-1], precision)} Z")
        else:
            parts.append(f"M{_fmt(coords, precision)}")
    return " ".join(parts)


def _preview_color(name: str) -> str:
    hue = int(hashlib.md5(name.encode()).hexdigest()[:4], 16) % 360 / 360.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.7, 0.5)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def sheet_to_svg(width: float, height: float, items: list[PlacedPart], preview: bool = False) -> str:
    unit = max(width, height)

    def up(geom):
        """Internal frame is y-down; the file uses y-up numbers (origin bottom-left) under a flipping transform."""
        return affinity.affine_transform(geom, [1, 0, 0, -1, 0, height])

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        f'width="{width:g}mm" height="{height:g}mm" viewBox="0 0 {width:g} {height:g}">',
    ]
    flip = f'transform="matrix(1 0 0 -1 0 {height:g})"'
    if preview:
        out.append(f'<rect x="0" y="0" width="{width:g}" height="{height:g}" fill="#ffffff"/>')
    out.append(
        f'<g id="SHEET" {flip} inkscape:groupmode="layer" inkscape:label="SHEET" fill="none" stroke="#999999" '
        f'stroke-width="{unit / 800:.3f}"><rect x="0" y="0" width="{width:g}" height="{height:g}"/></g>'
    )
    if preview:
        out.append(f'<g id="BODY" {flip} stroke="none">')
        for item in items:
            out.append(f'<path d="{polygon_path_d(up(item.outline))}" fill="{_preview_color(item.name)}" '
                       f'fill-rule="evenodd"><title>{escape(item.name)}</title></path>')
        out.append("</g>")

    sw = f"{unit / 1000:.3f}" if preview else "0.1"
    out.append(f'<g id="ENGRAVE" {flip} inkscape:groupmode="layer" inkscape:label="ENGRAVE" stroke="#0000ff" '
               f'stroke-width="{sw}" fill="none">')
    for item in items:
        for fill in item.fills:
            out.append(f'<path d="{polygon_path_d(up(fill))}" fill="#0000ff" fill-opacity="{0.35 if preview else 1}" '
                       f'fill-rule="evenodd"{"" if preview else " stroke=" + chr(34) + "none" + chr(34)}/>')
        if item.engrave:
            out.append(f'<path d="{lines_path_d([up(g) for g in item.engrave])}"/>')
    out.append("</g>")

    out.append(f'<g id="CUT" {flip} inkscape:groupmode="layer" inkscape:label="CUT" stroke="#ff0000" stroke-width="{sw}" fill="none">')
    for item in items:
        if item.cut:
            out.append(f'<path d="{lines_path_d([up(g) for g in item.cut])}"><title>{escape(item.name)}</title></path>')
    out.append("</g></svg>")
    return "\n".join(out)


def sheet_to_dxf(width: float, height: float, items: list[PlacedPart]) -> bytes:
    doc = ezdxf.new("R2010")
    doc.units = 4
    doc.layers.add("SHEET", color=8)
    doc.layers.add("ENGRAVE", color=5)
    doc.layers.add("CUT", color=1)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (width, 0), (width, height), (0, height)], close=True, dxfattribs={"layer": "SHEET"})

    def flip(coords):
        return [(x, height - y) for x, y in coords]

    def add_line(ls: LineString, layer: str) -> None:
        coords = list(ls.coords)
        closed = len(coords) > 3 and ls.is_closed
        msp.add_lwpolyline(flip(coords[:-1] if closed else coords), close=closed, dxfattribs={"layer": layer})

    for item in items:
        for fill in item.fills:
            for poly in _polygons(fill):
                # boundaries as plain polylines: many CAM/laser programs ignore HATCH entities
                for ring in [poly.exterior, *poly.interiors]:
                    msp.add_lwpolyline(flip(list(ring.coords)[:-1]), close=True, dxfattribs={"layer": "ENGRAVE"})
                hatch = msp.add_hatch(color=5, dxfattribs={"layer": "ENGRAVE"})
                hatch.paths.add_polyline_path(flip(list(poly.exterior.coords)[:-1]), is_closed=True,
                                              flags=ezdxf.const.BOUNDARY_PATH_EXTERNAL)
                for hole in poly.interiors:
                    hatch.paths.add_polyline_path(flip(list(hole.coords)[:-1]), is_closed=True)
        for ls in item.engrave:
            add_line(ls, "ENGRAVE")
        for ls in item.cut:
            add_line(ls, "CUT")
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")
