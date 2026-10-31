"""FastAPI backend: uploads, nesting jobs, sheet previews and SVG/DXF/ZIP export."""
from __future__ import annotations

import io
import os
import secrets
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .nester import NestCancelled, NestParams, NestResult, SheetSpec, nest
from .parser import (ParseError, Parsed, Part, ROLE_CUT, ROLE_ENGRAVE, ROLE_IGNORE, build_parts, lines_path_d,
                     parse_file, place_part, polygon_path_d, sheet_to_dxf, sheet_to_svg)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Limits protect a shared instance; raise them with environment variables for private use.
MAX_UPLOAD_FILES = _env_int("NEST_MAX_UPLOAD_FILES", 100)        # files in one upload request
MAX_FILE_BYTES = _env_int("NEST_MAX_FILE_MB", 50) * 1024 * 1024  # size of one file
MAX_PARTS_PER_SESSION = _env_int("NEST_MAX_PARTS", 2000)         # distinct parts kept per visitor
MAX_PIECES_PER_RUN = _env_int("NEST_MAX_PIECES", 5000)           # parts times quantity in one nesting run
MAX_SHEET_ROWS = 20
MAX_JOBS = 20                 # kept per session
SESSION_COOKIE = "nest_sid"
SESSION_TTL = 6 * 3600        # idle seconds before a session (and its uploads) is dropped
MAX_SESSIONS = 500
MAX_PARALLEL_NESTS = 2        # nesting is CPU heavy: extra jobs wait in a queue

app = FastAPI(title="True-Shape Nesting")

nest_slots = threading.BoundedSemaphore(MAX_PARALLEL_NESTS)


class Job:
    def __init__(self, parts: dict[str, Part]):
        self.id = uuid.uuid4().hex[:12]
        self.parts = parts
        self.status = "queued"
        self.progress = 0.0
        self.info: dict = {"stage": "queued"}
        self.error: Optional[str] = None
        self.result: Optional[NestResult] = None
        self.cancel = threading.Event()

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")


class SourceFile:
    """An uploaded file kept in memory so its pen/layer roles can be changed and parts rebuilt."""

    def __init__(self, name: str, parsed: Parsed):
        self.id = uuid.uuid4().hex[:8]
        self.name = name
        self.parsed = parsed
        self.roles: dict[str, str] = {}
        self.part_ids: list[str] = []


class Session:
    """Everything one visitor uploaded or computed; visitors never see each other's data."""

    def __init__(self):
        self.parts: dict[str, Part] = {}
        self.sources: dict[str, SourceFile] = {}
        self.jobs: "OrderedDict[str, Job]" = OrderedDict()
        self.lock = threading.Lock()
        self.last_seen = time.time()

    @property
    def busy(self) -> bool:
        return any(j.active for j in self.jobs.values())


sessions: dict[str, Session] = {}
sessions_lock = threading.Lock()


def _purge_sessions(now: float) -> None:
    """Drop idle sessions and, if still too many, the least recently used ones. Caller holds sessions_lock."""
    for sid in [sid for sid, s in sessions.items() if now - s.last_seen > SESSION_TTL and not s.busy]:
        del sessions[sid]
    while len(sessions) > MAX_SESSIONS:
        oldest = min((sid for sid, s in sessions.items() if not s.busy), key=lambda k: sessions[k].last_seen, default=None)
        if oldest is None:
            break
        del sessions[oldest]


def get_session(request: Request, response: Response) -> Session:
    now = time.time()
    sid = request.cookies.get(SESSION_COOKIE)
    with sessions_lock:
        _purge_sessions(now)
        session = sessions.get(sid) if sid else None
        if session is None:
            sid = secrets.token_urlsafe(24)
            session = sessions[sid] = Session()
        session.last_seen = now
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TTL, httponly=True, samesite="lax", secure=secure)
    return session


# ----------------------------------------------------------------------- parts

def _extra_cuts(part: Part) -> int:
    """Open cut lines that are not part of the outline or its holes (e.g. fold slits)."""
    zone = part.polygon.boundary.buffer(0.05)
    return sum(1 for ls in part.cut if not ls.is_closed and not zone.covers(ls))


def part_json(part: Part) -> dict:
    return {
        "id": part.id,
        "name": part.name,
        "source": part.source,
        "width": round(part.width, 2),
        "height": round(part.height, 2),
        "area": round(part.area, 2),
        "path": polygon_path_d(part.polygon, 2),
        "cut_path": lines_path_d(part.cut, 2),
        "engrave_path": lines_path_d(part.engrave, 2),
        "fill_path": " ".join(polygon_path_d(f, 2) for f in part.fills),
        "has_engrave": bool(part.engrave or part.fills),
        "extra_cuts": _extra_cuts(part),
    }


def _rebuild(src: SourceFile, roles: dict[str, str]) -> tuple[list[Part], list[dict]]:
    parts, warnings = build_parts(src.name, src.parsed, roles)
    for p in parts:
        p.file_id = src.id
    return parts, warnings


def _install(sess: Session, src: SourceFile, roles: dict[str, str], parts: list[Part]) -> None:
    with sess.lock:
        for pid in src.part_ids:
            sess.parts.pop(pid, None)
        for p in parts:
            sess.parts[p.id] = p
        src.roles = roles
        src.part_ids = [p.id for p in parts]
        sess.sources[src.id] = src


def file_json(src: SourceFile, parts: list[Part], warnings: list[dict]) -> dict:
    styles = [{**s, "role": src.roles.get(s["key"], s["role"])} for s in src.parsed.styles()]
    return {"id": src.id, "name": src.name, "styles": styles, "warnings": warnings,
            "parts": [part_json(p) | {"file_id": src.id} for p in parts]}


@app.post("/api/upload")
def upload(files: list[UploadFile] = File(...), sess: Session = Depends(get_session)):
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, "too_many_files")
    added, errors = [], []
    for f in files:
        data = f.file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            errors.append({"file": f.filename, "code": "too_large", "detail": str(MAX_FILE_BYTES // (1024 * 1024))})
            continue
        with sess.lock:
            stored = len(sess.parts)
        if stored >= MAX_PARTS_PER_SESSION:
            errors.append({"file": f.filename, "code": "too_many_parts", "detail": str(MAX_PARTS_PER_SESSION)})
            continue
        try:
            src = SourceFile(f.filename or "", parse_file(f.filename or "", data))
            parts, warnings = _rebuild(src, {})
        except ParseError as exc:
            errors.append({"file": f.filename, "code": exc.code, "detail": exc.detail})
            continue
        except Exception as exc:  # malformed input must not break the batch
            errors.append({"file": f.filename, "code": "failed", "detail": str(exc)})
            continue
        _install(sess, src, {}, parts)
        added.append(file_json(src, parts, warnings))
    return {"files": added, "errors": errors}


class RolesRequest(BaseModel):
    roles: dict[str, str]


@app.post("/api/files/{file_id}/roles")
def set_roles(file_id: str, req: RolesRequest, sess: Session = Depends(get_session)):
    src = sess.sources.get(file_id)
    if src is None:
        raise HTTPException(404, "file_not_found")
    if any(r not in (ROLE_CUT, ROLE_ENGRAVE, ROLE_IGNORE) for r in req.roles.values()):
        raise HTTPException(400, "bad_role")
    try:
        parts, warnings = _rebuild(src, req.roles)
    except ParseError as exc:
        raise HTTPException(400, "no_shape")
    _install(sess, src, req.roles, parts)
    return file_json(src, parts, warnings)


@app.delete("/api/parts/{part_id}")
def delete_part(part_id: str, sess: Session = Depends(get_session)):
    with sess.lock:
        part = sess.parts.pop(part_id, None)
        src = sess.sources.get(part.file_id) if part else None
        if src and part_id in src.part_ids:
            src.part_ids.remove(part_id)
            if not src.part_ids:
                sess.sources.pop(src.id, None)
    return {"ok": True}


@app.delete("/api/parts")
def clear_parts(sess: Session = Depends(get_session)):
    with sess.lock:
        sess.parts.clear()
        sess.sources.clear()
    return {"ok": True}


# ----------------------------------------------------------------------- nesting

class PartRequest(BaseModel):
    id: str
    quantity: int = Field(ge=0, le=100000)


class SheetRequest(BaseModel):
    width: float = Field(gt=0, le=100000)
    height: float = Field(gt=0, le=100000)
    quantity: int = Field(default=1, ge=1, le=100000)
    auto: bool = False


class NestRequest(BaseModel):
    parts: list[PartRequest]
    sheets: list[SheetRequest] = Field(min_length=1, max_length=MAX_SHEET_ROWS)
    spacing: float = Field(default=1.5, ge=0, le=100)
    margin: float = Field(default=5.0, ge=0, le=1000)
    rotation_step: int = 0
    resolution: float = Field(default=0.5, ge=0.1, le=5)
    strategy: str = "smallest"
    optimize: int = Field(default=1, ge=1, le=3)
    engrave_together: bool = False


def _run_job(job: Job, parts: list[tuple[Part, int]], sheets: list[SheetSpec], params: NestParams) -> None:
    def progress(value: float, info: dict) -> None:
        job.progress, job.info = value, info

    while not nest_slots.acquire(timeout=0.5):
        if job.cancel.is_set():
            job.status, job.info = "cancelled", {"stage": "cancelled"}
            return
    job.status, job.info = "running", {"stage": "starting"}
    try:
        job.result = nest(parts, sheets, params, progress=progress, cancelled=job.cancel.is_set)
        job.status = "done"
        job.progress = 1.0
        job.info = {"stage": "done"}
    except NestCancelled:
        job.status, job.info = "cancelled", {"stage": "cancelled"}
    except Exception as exc:
        job.status, job.error, job.info = "error", str(exc), {"stage": "error"}
    finally:
        nest_slots.release()


@app.post("/api/nest")
def start_nest(req: NestRequest, sess: Session = Depends(get_session)):
    if req.rotation_step not in (0, 90, 45, 15):
        raise HTTPException(400, "bad_rotation")
    if req.strategy not in ("smallest", "custom"):
        raise HTTPException(400, "bad_strategy")
    with sess.lock:
        selected = [(sess.parts[p.id], p.quantity) for p in req.parts if p.id in sess.parts and p.quantity > 0]
    if not selected:
        raise HTTPException(400, "no_parts")
    if sum(qty for _, qty in selected) > MAX_PIECES_PER_RUN:
        raise HTTPException(400, "too_many_pieces")

    job = Job({p.id: p for p, _ in selected})
    sheets = [SheetSpec(s.width, s.height, None if s.auto else s.quantity) for s in req.sheets]
    params = NestParams(req.spacing, req.margin, req.rotation_step, req.resolution, req.strategy, req.optimize,
                        req.engrave_together)
    with sess.lock:
        sess.jobs[job.id] = job
        while len(sess.jobs) > MAX_JOBS:
            sess.jobs.popitem(last=False)
    threading.Thread(target=_run_job, args=(job, selected, sheets, params), daemon=True).start()
    return {"job_id": job.id}


def _get_job(sess: Session, job_id: str) -> Job:
    job = sess.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job_not_found")
    return job


def _sheet_filename(sheet, ext: str) -> str:
    return f"sheet_{sheet.index:02d}_{sheet.width:g}x{sheet.height:g}mm.{ext}"


def _sheet_items(job: Job, sheet) -> list:
    return [place_part(job.parts[pl.part_id], pl.angle, pl.tx, pl.ty, pl.name) for pl in sheet.placements]


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, sess: Session = Depends(get_session)):
    job = _get_job(sess, job_id)
    payload = {"status": job.status, "progress": job.progress, "info": job.info, "error": job.error}
    if job.result:
        payload["result"] = {
            "elapsed": round(job.result.elapsed, 2),
            "trials": job.result.trials,
            "best_trial": job.result.best_trial,
            "sheets": [{
                "index": s.index,
                "width": s.width,
                "height": s.height,
                "utilization": round(s.utilization, 1),
                "free_right": round(s.free_right) if s.free_right else 0,
                "engraved": s.engraved,
                "count": len(s.placements),
                "filename": _sheet_filename(s, "svg"),
            } for s in job.result.sheets],
            "unplaced": [{"name": job.parts[pid].name, "count": n} for pid, n in job.result.unplaced.items()],
        }
    return payload


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str, sess: Session = Depends(get_session)):
    _get_job(sess, job_id).cancel.set()
    return {"ok": True}


def _get_sheet(job: Job, index: int):
    if job.result:
        for s in job.result.sheets:
            if s.index == index:
                return s
    raise HTTPException(404, "sheet_not_found")


@app.get("/api/jobs/{job_id}/sheets/{index}.svg")
def sheet_svg(job_id: str, index: int, preview: bool = False, sess: Session = Depends(get_session)):
    job = _get_job(sess, job_id)
    sheet = _get_sheet(job, index)
    svg = sheet_to_svg(sheet.width, sheet.height, _sheet_items(job, sheet), preview=preview)
    headers = {} if preview else {"Content-Disposition": f'attachment; filename="{_sheet_filename(sheet, "svg")}"'}
    return Response(svg, media_type="image/svg+xml", headers=headers)


@app.get("/api/jobs/{job_id}/sheets/{index}.dxf")
def sheet_dxf(job_id: str, index: int, sess: Session = Depends(get_session)):
    job = _get_job(sess, job_id)
    sheet = _get_sheet(job, index)
    data = sheet_to_dxf(sheet.width, sheet.height, _sheet_items(job, sheet))
    return Response(data, media_type="application/dxf",
                    headers={"Content-Disposition": f'attachment; filename="{_sheet_filename(sheet, "dxf")}"'})


@app.get("/api/jobs/{job_id}/download.zip")
def download_zip(job_id: str, format: str = Query("svg", pattern="^(svg|dxf|both)$"),
                 sess: Session = Depends(get_session)):
    job = _get_job(sess, job_id)
    if not job.result or not job.result.sheets:
        raise HTTPException(404, "no_sheets")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for sheet in job.result.sheets:
            items = _sheet_items(job, sheet)
            if format in ("svg", "both"):
                zf.writestr(_sheet_filename(sheet, "svg"), sheet_to_svg(sheet.width, sheet.height, items))
            if format in ("dxf", "both"):
                zf.writestr(_sheet_filename(sheet, "dxf"), sheet_to_dxf(sheet.width, sheet.height, items))
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="nesting_{format}.zip"'})


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
