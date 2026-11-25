"""xTool Studio project (.xs): a zip of JSON files, one canvas (panel) per work area.

A canvas lists its elements with a path (SVG path data in mm, inline or in the shared vector store) and a
2x3 matrix that places it on the canvas. Whether a path is cut or engraved is not a matter of its colour but
of the processing profile bound to it in the device file, so the roles come from there.
"""
from __future__ import annotations

import io
import json
import math
import zipfile
from dataclasses import dataclass

from svgelements import Matrix, Path

from .parser import (PX_TO_MM, ROLE_CUT, ROLE_ENGRAVE, ROLE_IGNORE, ROLE_SCORE, Linework, ParseError, Parsed, _collect,
                     _flatten_svg_subpath, _hex, _is_dark, _is_red, _is_white, color_name)

MAX_UNCOMPRESSED = 150 * 1024 * 1024      # declared size of everything inside the archive
MAX_ELEMENTS = 20000                      # drawn elements in one project
CLOSE_TOL = 0.05

PROCESS_LABEL = {
    "VECTOR_CUTTING": "cut",
    "VECTOR_ENGRAVING": "score",
    "FILL_VECTOR_ENGRAVING": "fill engraving",
    "BITMAP_ENGRAVING": "bitmap engraving",
}


@dataclass
class XsCanvas:
    name: str
    parsed: Parsed
    skipped: int          # elements that are not vector paths (text, images)


def _read_json(zf: zipfile.ZipFile, name: str):
    try:
        with zf.open(name) as f:
            return json.loads(f.read())
    except KeyError:
        return None
    except (ValueError, zipfile.BadZipFile) as exc:
        raise ParseError("invalid_xs", name) from exc


def _process_types(devices: list[dict], canvas_id: str) -> tuple[dict[str, str], set[str]]:
    """Processing type per element id, and the ids switched off for processing, from the device files."""
    types: dict[str, str] = {}
    ignored: set[str] = set()
    for dev in devices:
        modes = ((dev.get("processing") or {}).get(canvas_id) or {}).get("modes") or {}
        for mode in modes.values():
            ignored.update(mode.get("ignoredDisplayIds") or [])
            for binding in mode.get("bindings") or []:
                ptype = binding.get("processingType")
                for eid in binding.get("displayIds") or []:
                    types.setdefault(eid, binding.get("_type") or ptype or "")
    return types, ignored


def parse_xs(data: bytes) -> list[XsCanvas]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ParseError("invalid_xs") from exc
    with zf:
        if sum(i.file_size for i in zf.infolist()) > MAX_UNCOMPRESSED:
            raise ParseError("xs_too_big")
        project = _read_json(zf, "project.json")
        if not isinstance(project, dict):
            raise ParseError("invalid_xs", "project.json")
        profiles = (_read_json(zf, "profiles.json") or {}).get("profiles", {})

        names = [n for n in zf.namelist() if n.startswith("devices/") and n.endswith(".json")]
        active = f"devices/device-{project.get('activeDeviceId')}.json"
        devices = [d for n in sorted(names, key=lambda n: n != active) if isinstance(d := _read_json(zf, n), dict)]
        for dev in devices:   # resolve every binding to the processing type of its profile
            for per_canvas in (dev.get("processing") or {}).values():
                for mode in (per_canvas.get("modes") or {}).values():
                    for binding in mode.get("bindings") or []:
                        binding["_type"] = (profiles.get(binding.get("baseProfileId")) or {}).get("processingType", "")

        vectors: dict[str, str] = {}
        index = _read_json(zf, "vectors/svg/index.json") or {}
        if index:
            for n in zf.namelist():
                if n.startswith("vectors/svg/data-") and n.endswith(".json"):
                    vectors.update((_read_json(zf, n) or {}).get("entries", {}))

        project_name = str(project.get("projectName") or "xtool")
        canvas_ids = (project.get("modules") or {}).get("canvases") or []
        out: list[XsCanvas] = []
        total = 0
        for number, canvas_id in enumerate(canvas_ids, 1):
            meta = _read_json(zf, f"canvases/{canvas_id}.json") or {}
            if meta.get("hidden"):
                continue
            displays = []
            for n in sorted(i for i in zf.namelist() if i.startswith(f"canvases/{canvas_id}/displays-")):
                displays.extend((_read_json(zf, n) or {}).get("displays", []))
            total += len(displays)
            if total > MAX_ELEMENTS:
                raise ParseError("xs_too_big")
            types, ignored = _process_types(devices, canvas_id)
            parsed, skipped = _canvas(displays, vectors, types, ignored)
            if parsed.lines:
                out.append(XsCanvas(f"{project_name} panel {number}.xs", parsed, skipped))
    if not out:
        raise ParseError("no_shape")
    return out


def _canvas(displays: list[dict], vectors: dict[str, str], types: dict[str, str], ignored: set[str],
            by_colour: bool = False) -> tuple[Parsed, int]:
    lines: list[Linework] = []
    hatch: list[Linework] = []
    skipped = 0
    group = 0
    for el in displays:
        if el.get("visible") is False or el.get("visibleState") is False:
            continue
        d = el.get("dPath") or vectors.get((el.get("vectorRef") or {}).get("vectorHash", ""))
        matrix = el.get("transform")
        if not d or not isinstance(matrix, list) or len(matrix) != 6:
            skipped += 1      # text, images and the like
            continue
        try:
            path = abs(Path(d) * Matrix(*matrix) * Matrix.scale(1 / PX_TO_MM))
        except Exception:
            skipped += 1
            continue
        group += 1
        ptype = types.get(el.get("id"), "")
        color = str(el.get("layerColor") or el.get("originColor") or "#000000")
        rgb = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) if len(color.lstrip("#")) == 6 else (0, 0, 0)
        fill = (el.get("fill") or {}).get("visible")
        if el.get("id") in ignored or _is_white(rgb):
            role = ROLE_IGNORE
        elif ptype == "VECTOR_CUTTING":
            role = ROLE_CUT
        elif ptype == "VECTOR_ENGRAVING":      # a line engraved with less power than a cut
            # .xcs files default every outline to this until it is changed, so there the colour decides
            role = ROLE_CUT if by_colour and not fill and (_is_red(rgb) or _is_dark(rgb)) else ROLE_SCORE
        elif ptype:
            role = ROLE_ENGRAVE                # fill and bitmap engraving: an area
        else:
            role = ROLE_CUT if _is_red(rgb) or _is_dark(rgb) else (ROLE_ENGRAVE if fill else ROLE_SCORE)   # not bound to a process
        hexcolor = _hex(rgb)
        label = PROCESS_LABEL.get(ptype, "")
        style = (f"{hexcolor}|{ptype}", color_name(rgb), hexcolor, label)
        for sub in path.as_subpaths():
            pts, is_closed = _flatten_svg_subpath(sub)
            closed = (is_closed or bool(el.get("isClosePath"))) and len(pts) >= 3
            if closed and abs(pts[0][0] - pts[-1][0]) + abs(pts[0][1] - pts[-1][1]) < CLOSE_TOL:
                pts = pts[:-1]
            if closed and len(pts) >= 3:
                filled = role == ROLE_ENGRAVE and bool(fill)
                if filled:
                    hatch.append(Linework(pts, True, role, style, group))
                lines.append(Linework(pts, True, role, style, filled=filled))
            else:
                _collect(pts, role, style, lines)
    return Parsed(lines, hatch), skipped


def parse_xcs(data: bytes, project_name: str = "xtool") -> list[XsCanvas]:
    """xTool Creative Space project (.xcs): plain JSON, one entry per canvas, processing in `device.data`."""
    try:
        doc = json.loads(data)
    except ValueError as exc:
        raise ParseError("invalid_xs") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("canvas"), list):
        raise ParseError("invalid_xs", "canvas")
    per_canvas = {}
    try:
        per_canvas = {k: v for k, v in ((doc.get("device") or {}).get("data") or {}).get("value") or []}
    except (TypeError, ValueError):
        pass
    out: list[XsCanvas] = []
    total = 0
    for number, canvas in enumerate(doc["canvas"], 1):
        displays = canvas.get("displays") or []
        total += len(displays)
        if total > MAX_ELEMENTS:
            raise ParseError("xs_too_big")
        types: dict[str, str] = {}
        ignored: set[str] = set()
        for eid, cfg in (((per_canvas.get(canvas.get("id")) or {}).get("displays") or {}).get("value") or []):
            types[eid] = cfg.get("processingType") or ""
            if cfg.get("processIgnore"):
                ignored.add(eid)
        placed = []
        for el in displays:
            d = el.get("dPath")
            if not d:
                placed.append(el)      # counted as skipped
                continue
            try:
                placed.append({**el, "transform": _xcs_matrix(d, el)})
            except Exception:
                placed.append({k: v for k, v in el.items() if k != "dPath"})
        parsed, skipped = _canvas(placed, {}, types, ignored, by_colour=True)
        if parsed.lines:
            out.append(XsCanvas(f"{project_name} panel {number}.xcs", parsed, skipped))
    if not out:
        raise ParseError("no_shape")
    return out


def _xcs_matrix(d: str, el: dict) -> list[float]:
    """Element placement. The path is shifted so its bounding box starts at the display origin, then it gets
    the display's scale, rotation and skew (the editor's matrix: a skew of pi flips the y axis, which is how
    the bottom edge ends up at y) and the origin is moved to (x, y), all in mm."""
    x0, y0, _, _ = Path(d).bbox()
    sx, sy = float(el["scale"]["x"]), float(el["scale"]["y"])
    skew = el.get("skew") or {}
    rot = math.radians(float(el.get("angle") or 0))
    skx, sky = float(skew.get("x") or 0), float(skew.get("y") or 0)
    a, b = math.cos(rot + sky) * sx, math.sin(rot + sky) * sx
    c, dd = -math.sin(rot - skx) * sy, math.cos(rot - skx) * sy
    return [a, b, c, dd, float(el["x"]) - a * x0 - c * y0, float(el["y"]) - b * x0 - dd * y0]
