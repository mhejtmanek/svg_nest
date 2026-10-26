"""True-shape nesting: vector preparation, rasterisation and FFT-convolution first-fit-decreasing."""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import scipy.fft
from PIL import Image, ImageDraw
from scipy.ndimage import binary_dilation, binary_erosion
from scipy.signal import fftconvolve
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon

from .parser import Part, transform_polygon

COLLISION_THRESHOLD = 0.5   # overlap counts are integers; float32 FFT noise stays far below this
VERIFY_ATTEMPTS = 5
MAX_AUTO_SHEETS = 1000
# (part ordering, weight of the x extent, weight of the y extent, reward for touching placed parts)
TRIALS = [("area", 1, 1, 0), ("long", 1, 1, 0), ("area", 1, 1, 3), ("area", 1, 0.4, 0), ("area", 0.4, 1, 0),
          ("bbox", 1, 1, 3), ("height", 1, 0.5, 0), ("width", 0.5, 1, 0), ("long", 1, 0.4, 3), ("long", 0.4, 1, 3),
          ("bbox", 1, 0.4, 0), ("perimeter", 1, 1, 3)]
TRIAL_COUNT = {1: 1, 2: 6, 3: 12}
ORDER_KEYS = {
    "area": lambda p: p.area,
    "long": lambda p: max(p.width, p.height),
    "bbox": lambda p: p.width * p.height,
    "height": lambda p: p.height,
    "width": lambda p: p.width,
    "perimeter": lambda p: p.polygon.length,
}
SAFETY_PIXELS = 0.1


class NestCancelled(Exception):
    pass


@dataclass
class SheetSpec:
    width: float
    height: float
    quantity: Optional[int] = None  # None = unlimited (Auto)


@dataclass
class NestParams:
    spacing: float = 1.5
    margin: float = 5.0
    rotation_step: int = 0          # 0 = none, otherwise 90 / 45 / 15
    resolution: float = 0.5
    strategy: str = "smallest"      # "smallest" or "custom"
    optimize: int = 1               # 1 = one fast pass, 2 = thorough, 3 = maximum (several part orders / gravities)

    @property
    def angles(self) -> list[int]:
        return [0] if self.rotation_step <= 0 else list(range(0, 360, self.rotation_step))


@dataclass
class Placement:
    part_id: str
    name: str
    instance: int
    angle: float
    tx: float
    ty: float


@dataclass
class SheetResult:
    index: int
    width: float
    height: float
    placements: list[Placement]
    utilization: float
    free_right: float = 0.0     # width (mm) of the untouched strip on the right of the last sheet


@dataclass
class NestResult:
    sheets: list[SheetResult]
    unplaced: dict[str, int]
    elapsed: float
    trials: int = 1
    best_trial: int = 1


@dataclass
class _Variant:
    angle: int
    mask: np.ndarray        # float32, 1 = occupied (inflated part)
    mask_bool: np.ndarray
    kernel: np.ndarray      # flipped mask for correlation via convolution
    edge_kernel: np.ndarray # flipped one-pixel outline of the mask, used to measure contact with placed parts
    min_x: float            # bounds of the rotated inflated polygon (rotated frame)
    min_y: float

    @property
    def shape(self) -> tuple[int, int]:
        return self.mask.shape


def _rasterize(geom, width_px: int, height_px: int, res: float) -> np.ndarray:
    """Conservative rasterisation: every touched pixel is set, holes keep their free interior."""
    img = Image.new("L", (width_px, height_px), 0)
    draw = ImageDraw.Draw(img)
    polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    for poly in polys:
        draw.polygon([(x / res, y / res) for x, y in poly.exterior.coords], fill=1, outline=1)
        for hole in poly.interiors:
            pts = [(x / res, y / res) for x, y in hole.coords]
            draw.polygon(pts, fill=0)
            draw.line(pts, fill=1, width=1)
    return np.asarray(img, dtype=np.float32)


def inflation(spacing: float, res: float) -> float:
    """Offset applied to every part: half the spacing plus a safety allowance for raster edge error."""
    return spacing / 2.0 + SAFETY_PIXELS * res


class _MaskCache:
    def __init__(self, spacing: float, res: float, angles: list[int]):
        self.inflate = inflation(spacing, res)
        self.res = res
        self.angles = angles
        self._cache: dict[str, list[_Variant]] = {}

    def variants(self, part: Part) -> list[_Variant]:
        if part.id in self._cache:
            return self._cache[part.id]
        inflated = part.polygon.buffer(self.inflate, join_style="round", quad_segs=8)
        seen: set[tuple] = set()
        result: list[_Variant] = []
        for angle in self.angles:
            rotated = affinity.rotate(inflated, angle, origin=(0, 0))
            min_x, min_y, max_x, max_y = rotated.bounds
            shifted = affinity.translate(rotated, -min_x, -min_y)
            w = int(math.ceil((max_x - min_x) / self.res)) + 1
            h = int(math.ceil((max_y - min_y) / self.res)) + 1
            mask = _rasterize(shifted, w, h, self.res)
            key = (mask.shape, hash(mask.tobytes()))
            if key in seen:
                continue
            seen.add(key)
            as_bool = mask > 0.5
            edge = as_bool & ~binary_erosion(as_bool)
            result.append(_Variant(
                angle=angle, mask=mask, mask_bool=as_bool,
                kernel=np.ascontiguousarray(mask[::-1, ::-1]),
                edge_kernel=np.ascontiguousarray(edge[::-1, ::-1].astype(np.float32)),
                min_x=min_x, min_y=min_y,
            ))
        self._cache[part.id] = result
        return result


@dataclass
class _Instance:
    part: Part
    number: int


def _find_position(grid: np.ndarray, variants: list[_Variant], wx: float = 1.0, wy: float = 1.0, contact: float = 0.0):
    """Best (row, col) over all rotations, packing towards the bottom-left corner."""
    grid_h, grid_w = grid.shape
    best = None  # (cost, variant, row, col)
    occupied = binary_dilation(grid > 0.5, iterations=2).astype(np.float32) if contact else None
    for v in variants:
        h, w = v.shape
        if h > grid_h or w > grid_w:
            continue
        if best is not None and not contact and best[0] <= wx * w + wy * h:
            continue
        collisions = fftconvolve(grid, v.kernel, mode="valid")
        rows, cols = np.nonzero(collisions < COLLISION_THRESHOLD)
        if rows.size == 0:
            continue
        cost = wx * (cols + w) + wy * (grid_h - rows)   # right extent + height of the top edge above the bottom
        if contact:
            touching = fftconvolve(occupied, v.edge_kernel, mode="valid")
            cost = cost - contact * touching[rows, cols]
        for _ in range(VERIFY_ATTEMPTS):
            k = int(np.argmin(cost))
            r, c = int(rows[k]), int(cols[k])
            if not np.any(grid[r:r + h, c:c + w][v.mask_bool]):
                if best is None or cost[k] < best[0]:
                    best = (float(cost[k]), v, r, c)
                break
            cost[k] = np.inf
    return best


def _fill_sheet(spec: SheetSpec, params: NestParams, cache: _MaskCache, remaining: list[_Instance],
                tick: Callable[[], None], wx: float = 1.0, wy: float = 1.0, contact: float = 0.0) -> list[tuple[_Instance, Placement]]:
    res = params.resolution
    inflate = inflation(params.spacing, res)
    origin = params.margin - inflate
    usable_w = spec.width - 2 * params.margin + 2 * inflate
    usable_h = spec.height - 2 * params.margin + 2 * inflate
    grid_w, grid_h = int(usable_w / res), int(usable_h / res)
    if grid_w <= 0 or grid_h <= 0:
        return []

    grid = np.zeros((grid_h, grid_w), dtype=np.float32)
    free = grid_h * grid_w
    failed: set[str] = set()
    placed: list[tuple[_Instance, Placement]] = []

    for inst in remaining:
        tick()
        pid = inst.part.id
        if pid in failed:
            continue
        variants = cache.variants(inst.part)
        if not any(v.shape[0] <= grid_h and v.shape[1] <= grid_w and v.mask_bool.sum() <= free for v in variants):
            failed.add(pid)
            continue
        found = _find_position(grid, variants, wx, wy, contact)
        if found is None:
            failed.add(pid)  # the grid only gets fuller, so further copies cannot fit either
            continue
        _, v, r, c = found
        h, w = v.shape
        region = grid[r:r + h, c:c + w]
        np.maximum(region, v.mask, out=region)
        free = int(grid.size - np.count_nonzero(grid))
        x = origin + c * res
        y = origin + r * res
        placed.append((inst, Placement(pid, inst.part.name, inst.number, v.angle, x - v.min_x, y - v.min_y)))
    return placed


def _nest_once(parts: list[tuple[Part, int]], ordered: list[SheetSpec], params: NestParams, cache: _MaskCache,
               order: str, wx: float, wy: float, contact: float, report: Callable[[float, dict], None],
               tick: Callable[[], None]) -> tuple[list[SheetResult], list[_Instance]]:
    instances = [_Instance(p, k) for p, qty in parts for k in range(qty)]
    key = ORDER_KEYS[order]
    instances.sort(key=lambda i: -key(i.part))
    total = len(instances)
    remaining = list(instances)
    results: list[SheetResult] = []
    done = 0
    for spec in ordered:
        used = 0
        while remaining and (spec.quantity is None or used < spec.quantity) and used < MAX_AUTO_SHEETS:
            used += 1
            report(done / max(total, 1), {"stage": "sheet", "n": len(results) + 1,
                                          "width": spec.width, "height": spec.height})
            placed = _fill_sheet(spec, params, cache, remaining, tick, wx, wy, contact)
            if not placed:
                break  # identical copies would stay empty as well
            taken = {id(inst) for inst, _ in placed}
            remaining = [i for i in remaining if id(i) not in taken]
            done += len(placed)
            area = sum(inst.part.area for inst, _ in placed)
            results.append(SheetResult(
                index=len(results) + 1, width=spec.width, height=spec.height,
                placements=[pl for _, pl in placed],
                utilization=100.0 * area / (spec.width * spec.height),
            ))
        if not remaining:
            break
    return results, remaining


REPACK_ORDERS = ("height", "area", "long")
REPACK_WEIGHTS = ((1.0, 0.05), (1.0, 0.3))


def _extent_x(parts: dict[str, Part], placements: list[Placement]) -> float:
    return max(transform_polygon(parts[pl.part_id].polygon, pl.angle, pl.tx, pl.ty).bounds[2] for pl in placements)


def _repack_last(result: SheetResult, parts: dict[str, Part], params: NestParams, cache: _MaskCache,
                 tick: Callable[[], None]) -> SheetResult:
    """Re-pack the last sheet column by column from the left so the free area on the right stays in one piece."""
    spec = SheetSpec(result.width, result.height, None)
    count = len(result.placements)
    best = (_extent_x(parts, result.placements), result.placements)
    for order in REPACK_ORDERS:
        key = ORDER_KEYS[order]
        for wx, wy in REPACK_WEIGHTS:
            instances = [_Instance(parts[pl.part_id], pl.instance) for pl in result.placements]
            instances.sort(key=lambda i: -key(i.part))
            placed = _fill_sheet(spec, params, cache, instances, tick, wx, wy)
            if len(placed) != count:
                continue
            placements = [pl for _, pl in placed]
            extent = _extent_x(parts, placements)
            if extent < best[0] - 1e-6:
                best = (extent, placements)
    free = max(0.0, result.width - best[0] - params.margin)
    return SheetResult(index=result.index, width=result.width, height=result.height, placements=best[1],
                       utilization=result.utilization, free_right=free)


def _score(results: list[SheetResult], remaining: list[_Instance]) -> tuple:
    """Lower is better: everything placed, fewest sheets, then the weakest non-last sheet as full as possible."""
    sheet_area = sum(s.width * s.height for s in results)
    inner = [s.utilization for s in results[:-1]]
    weakest = min(inner) if inner else 100.0
    last = results[-1] if results else None
    last_used = last.utilization * last.width * last.height / 100.0 if last else 0.0
    return (len(remaining), round(sheet_area), len(results), -round(weakest, 1), last_used)


def nest(parts: list[tuple[Part, int]], sheets: list[SheetSpec], params: NestParams,
         progress: Optional[Callable[[float, dict], None]] = None,
         cancelled: Optional[Callable[[], bool]] = None) -> NestResult:
    started = time.time()
    if params.strategy == "smallest":
        ordered = sorted(sheets, key=lambda s: (s.quantity is None, s.width * s.height))
    else:
        ordered = list(sheets)

    cache = _MaskCache(params.spacing, params.resolution, params.angles)
    trials = TRIALS[:TRIAL_COUNT.get(params.optimize, 1)]

    def tick() -> None:
        if cancelled and cancelled():
            raise NestCancelled()

    best = None  # (score, results, remaining, trial number)
    with scipy.fft.set_workers(os.cpu_count() or 1):
        for number, (order, wx, wy, contact) in enumerate(trials, 1):
            def report(fraction: float, info: dict, number=number) -> None:
                if progress:
                    progress((number - 1 + fraction) / len(trials),
                             {**info, "trial": number, "trials": len(trials)})

            results, remaining = _nest_once(parts, ordered, params, cache, order, wx, wy, contact, report, tick)
            score = _score(results, remaining)
            if best is None or score < best[0]:
                best = (score, results, remaining, number)

    _, results, remaining, number = best
    if results:
        by_id = {p.id: p for p, _ in parts}
        with scipy.fft.set_workers(os.cpu_count() or 1):
            results[-1] = _repack_last(results[-1], by_id, params, cache, tick)
    unplaced: dict[str, int] = {}
    for inst in remaining:
        unplaced[inst.part.id] = unplaced.get(inst.part.id, 0) + 1
    if progress:
        progress(1.0, {"stage": "done"})
    return NestResult(sheets=results, unplaced=unplaced, elapsed=time.time() - started,
                      trials=len(trials), best_trial=number)
