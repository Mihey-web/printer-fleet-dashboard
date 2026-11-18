"""«Файлы на принтере»: the printer's stored print files and starting one (admin only).

Under /api/admin/: JWTAuthMiddleware lets only admins in (app/api/main.py).
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.services import printer_files

router = APIRouter(prefix="/api/admin", tags=["printer-files"])
logger = logging.getLogger(__name__)


def _fail(exc: Exception, code: int = 409):
    return JSONResponse(status_code=code, content={"detail": str(exc)})


@router.get("/printers/{printer_id}/files")
def printer_files_list(printer_id: str,
                       access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    try:
        return printer_files.list_files(printer_id)
    except LookupError as e:
        return _fail(e)


@router.get("/printers/{printer_id}/files/detail")
def printer_file_detail(printer_id: str, path: str = Query(..., max_length=1024),
                        access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    """Bambu: plates, the printer's AMS slots and the default slot per filament."""
    from app.services import printer_commands
    try:
        detail = printer_files.file_detail(printer_id, path)
    except LookupError as e:
        return _fail(e)
    from app.services import printer_registry
    prn = printer_registry.get_printer(printer_id) or {}
    if prn.get("kind") == "bambu":
        view = printer_commands._live_view(printer_id, prn)
        slots = printer_files.ams_slots(view.units) if view is not None else []
        detail["slots"] = slots
        for plate in detail.get("plates") or []:
            plate["mapping"] = printer_files.default_mapping(plate.get("filaments") or [], slots)
        detail["refusal"] = printer_files.bambu_start_refusal(view)
        if not detail.get("plates"):         # a project saved unsliced: no plate G-code to start
            detail["refusal"] = printer_files.MSG_NOT_SLICED
        if printer_commands.get_capability(printer_id) is False:
            detail["refusal"] = printer_files.MSG_NO_PRINT_CMDS
    else:
        detail["refusal"] = printer_files.creality_start_refusal(printer_commands._ws_live(printer_id, prn))
    return detail


_thumbs = {}


@router.get("/printers/{printer_id}/files/thumb")
def printer_file_thumb(printer_id: str, path: str = Query(..., max_length=1024),
                       access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    """Creality: the listing's thumbnail, re-encoded through job_previews (an image or nothing)."""
    from app.services import job_previews
    entry = printer_files.listed(printer_id, path)
    if entry is None:
        raise HTTPException(status_code=404, detail="Нет файла")
    cache = (printer_id, path, entry.get("mtime"))
    key = _thumbs.get(cache)
    target = job_previews.DIRECTORY / ("%s.png" % key) if key else None
    if target is None or not target.is_file():
        try:
            key = job_previews.save(printer_files.creality_thumb(printer_id, path))
        except (LookupError, ValueError, OSError):
            raise HTTPException(status_code=404, detail="Картинки нет")
        _thumbs[cache] = key
        if len(_thumbs) > 2000:
            _thumbs.clear()
        target = job_previews.DIRECTORY / ("%s.png" % key)
    return FileResponse(target, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@router.delete("/printers/{printer_id}/files")
def printer_file_delete(printer_id: str, request: Request, path: str = Query(..., max_length=1024),
                        access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    """One listed file off a Creality with Klipper's API (Moonraker), only while the printer stands."""
    from app.api.admin import _audit
    entry = printer_files.listed(printer_id, path) or {}
    r = printer_files.delete_file(printer_id, path)
    _audit(request, "printer_file_delete:%s:file=%s,st=%s" % (
        printer_id, str(entry.get("title") or "?")[:60].replace(":", " ").replace(",", " "), r.get("status", "rejected")))
    if not r["success"]:
        return JSONResponse(status_code=400, content={"detail": r["detail"], "status": r.get("status", "rejected")})
    return {"success": True, "status": r["status"], "detail": r["detail"]}


class PrintStartRequest(BaseModel):
    path: str
    plate: Optional[int] = None
    mapping: Optional[List[int]] = None
    bed_leveling: Optional[bool] = None
    timelapse: Optional[bool] = None


@router.post("/printers/{printer_id}/print")
def printer_print_start(printer_id: str, body: PrintStartRequest, request: Request,
                        access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    from app.api.admin import _audit
    params = {k: v for k, v in body.__dict__.items() if v is not None}
    r = printer_files.start(printer_id, params)
    entry = printer_files.listed(printer_id, body.path) or {}
    shown = ["file=%s" % str(entry.get("title") or "?")[:60].replace(":", " ").replace(",", " ")]
    for k in ("plate", "mapping", "bed_leveling", "timelapse"):
        if k in params:
            shown.append("%s=%s" % (k, "/".join(map(str, params[k])) if k == "mapping" else params[k]))
    shown.append("st=%s" % r.get("status", "rejected"))
    _audit(request, "printer_print:%s:%s" % (printer_id, ",".join(shown)))
    if not r["success"]:
        return JSONResponse(status_code=400, content={"detail": r["detail"], "status": r.get("status", "rejected")})
    return {"success": True, "status": r.get("status", "sent"), "detail": r["detail"],
            "replied": r.get("replied", False)}


# --- the shared pool per model (app/services/file_pool.py) ---------------------------------

@router.get("/file-pool")
def file_pool_groups(access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    from app.services import file_pool
    return {"groups": file_pool.groups(), "sync": file_pool.status()}


@router.get("/file-pool/sync")
def file_pool_sync_status(access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    from app.services import file_pool
    return file_pool.status()


@router.get("/file-pool/{group}")
def file_pool_files(group: str, access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    """Every file of the model: on which printers, and in the panel's own storage."""
    from app.services import file_pool
    try:
        pool = file_pool.build_pool(group)
    except LookupError as e:
        return _fail(e, 404)
    for f in pool["files"]:
        f["readable"] = bool(f.pop("sources")) and not f.get("sample")
    return pool


class PoolPlanRequest(BaseModel):
    files: Optional[List[str]] = None     # the ticked pool ids; none = every file


@router.post("/file-pool/{group}/plan")
def file_pool_plan(group: str, body: Optional[PoolPlanRequest] = None,
                   access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    """What «Синхронизировать» would copy: files → printers, sizes, the printers skipped and why."""
    from app.services import file_pool
    try:
        return file_pool.make_plan(group, body.files if body is not None else None)
    except LookupError as e:
        return _fail(e, 404)


class PoolSyncRequest(BaseModel):
    plan: str


@router.post("/file-pool/{group}/sync")
def file_pool_sync(group: str, body: PoolSyncRequest, request: Request,
                   access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    from app.api.admin import _audit
    from app.services import file_pool
    try:
        out = file_pool.start_sync(body.plan, group)       # the audit line names what is copied
    except ValueError as e:
        return _fail(e)
    rows = out.get("printers") or []
    _audit(request, "file_pool_sync:%s:files=%d,printers=%d" % (
        group, sum(r["files_total"] for r in rows), len(rows)))
    return out


@router.post("/file-pool/{group}/upload")
async def file_pool_upload(group: str, request: Request, name: str = Query(..., max_length=200),
                           access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    """A sliced file from the browser into the model's pool on the panel (raw body, not multipart)."""
    from app.api.admin import _audit
    from app.services import file_pool
    try:
        out = await file_pool.store_upload_async(group, name, request.stream())
    except LookupError as e:
        return _fail(e, 404)
    except ValueError as e:
        return _fail(e, 400)
    _audit(request, "file_pool_upload:%s:size=%d" % (group, out["size"]))
    return out


@router.delete("/file-pool/{group}/stored")
def file_pool_delete(group: str, request: Request, name: str = Query(..., max_length=200),
                     access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    """Remove a file from the panel's own storage (never from a printer)."""
    from app.api.admin import _audit
    from app.services import file_pool
    try:
        file_pool.delete_pi_file(group, name)
    except LookupError as e:
        return _fail(e, 404)
    except ValueError as e:
        return _fail(e, 400)
    _audit(request, "file_pool_delete:%s" % group)
    return {"success": True}
