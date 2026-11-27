import asyncio
import json
import logging
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from consumer_service import storage
from consumer_service.config import Settings
from consumer_service.security import is_authorized

logger = logging.getLogger("consumer_service.jobs")

router = APIRouter()

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000
_LIMIT_RE = re.compile(r"[0-9]{1,4}")


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _log_id(value: str | None) -> str:
    """Nilai header untuk log; dipotong dan di-repr karena belum tervalidasi."""
    if value is None:
        return "-"
    return repr(value[:80])


def _content_length(value: str | None) -> int | None:
    """Nilai header Content-Length, atau None bila tidak ada/tidak valid."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_limit(value: str | None) -> int:
    """Query `limit`: bilangan 0..MAX_LIST_LIMIT, default DEFAULT_LIST_LIMIT."""
    if value is None:
        return DEFAULT_LIST_LIMIT
    if not _LIMIT_RE.fullmatch(value) or int(value) > MAX_LIST_LIMIT:
        raise ValueError(f"limit harus bilangan 0..{MAX_LIST_LIMIT}")
    return int(value)


@router.post("/v1/jobs")
async def create_job(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    request_id = request.headers.get("x-request-id")
    pole_id = request.headers.get("x-pole-id")
    log_id = _log_id(request_id)

    # 1) token
    if not is_authorized(request.headers.get("authorization"), settings.consumer_token):
        logger.warning("request_id=%s 401 token salah atau tidak ada", log_id)
        return _error(401, "unauthorized")

    # 2) header
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        logger.warning("request_id=%s 400 content-type %r", log_id, content_type[:80])
        return _error(400, "Content-Type harus application/json")
    try:
        filename = storage.build_filename(request_id or "", pole_id or None)
    except ValueError as exc:
        logger.warning("request_id=%s pole_id=%s 400 %s", log_id, _log_id(pole_id), exc)
        return _error(400, str(exc))

    # request_id sudah tervalidasi oleh build_filename; pakai bentuk ternormalisasi
    request_id = (request_id or "").lower()

    # 3) ukuran: tolak dari Content-Length sebelum body dibaca, lalu cek ulang
    #    setelah dibaca untuk request tanpa Content-Length.
    declared = _content_length(request.headers.get("content-length"))
    if declared is not None and declared > settings.max_body_bytes:
        logger.warning(
            "request_id=%s 413 content-length %d > %d",
            request_id,
            declared,
            settings.max_body_bytes,
        )
        return _error(413, f"body melebihi {settings.max_body_bytes} byte")

    body = await request.body()
    if len(body) > settings.max_body_bytes:
        logger.warning(
            "request_id=%s 413 body %d byte > %d",
            request_id,
            len(body),
            settings.max_body_bytes,
        )
        return _error(413, f"body melebihi {settings.max_body_bytes} byte")

    # 4) UTF-8 + JSON (hanya dicek, body tidak diubah)
    try:
        json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        logger.warning("request_id=%s 400 body bukan JSON UTF-8: %s", request_id, exc)
        return _error(400, "body harus JSON valid ber-encoding UTF-8")

    lock: asyncio.Lock = request.app.state.jobs_lock
    async with lock:
        location = storage.find_existing(settings.jobs_root, request_id)
        if location is not None:
            logger.info("request_id=%s 200 duplicate di %s", request_id, location)
            return JSONResponse(
                status_code=200,
                content={
                    "request_id": request_id,
                    "status": "duplicate",
                    "location": location,
                },
            )
        try:
            await asyncio.to_thread(
                storage.write_job_atomic, settings.jobs_root, filename, body
            )
        except OSError:
            logger.exception("request_id=%s 500 gagal menulis %s", request_id, filename)
            return _error(500, "gagal menulis file job")

    logger.info("request_id=%s 201 queued file=%s", request_id, filename)
    return JSONResponse(
        status_code=201,
        content={"request_id": request_id, "status": "queued", "file": filename},
    )


@router.get("/v1/jobs/{request_id}")
async def get_job(request_id: str, request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    if not is_authorized(request.headers.get("authorization"), settings.consumer_token):
        logger.warning(
            "request_id=%s 401 token salah atau tidak ada", _log_id(request_id)
        )
        return _error(401, "unauthorized")
    try:
        status = storage.get_status(settings.jobs_root, request_id)
    except ValueError as exc:
        logger.warning("request_id=%s 400 %s", _log_id(request_id), exc)
        return _error(400, str(exc))
    request_id = request_id.lower()
    logger.info("request_id=%s status=%s", request_id, status)
    return JSONResponse(content={"request_id": request_id, "status": status})


@router.get("/v1/jobs")
async def list_jobs(request: Request) -> JSONResponse:
    """Daftar job per status, dibaca dari folder. Hanya membaca, tanpa lock.

    `count` adalah jumlah seluruh job berstatus itu; `jobs` berisi paling banyak
    `limit` job terlama. `limit=0` dipakai untuk meminta jumlahnya saja.
    """
    settings: Settings = request.app.state.settings
    if not is_authorized(request.headers.get("authorization"), settings.consumer_token):
        logger.warning("request_id=- 401 token salah atau tidak ada (daftar job)")
        return _error(401, "unauthorized")
    status = request.query_params.get("status", "")
    try:
        limit = _parse_limit(request.query_params.get("limit"))
        jobs, skipped = await asyncio.to_thread(
            storage.list_jobs, settings.jobs_root, status
        )
    except ValueError as exc:
        logger.warning("request_id=- 400 daftar job: %s", exc)
        return _error(400, str(exc))
    for name in skipped:
        logger.warning(
            "request_id=- nama file tidak sesuai pola, dilewati: %s", _log_id(name)
        )
    now = datetime.now(UTC)
    items = [
        {
            "request_id": job.request_id,
            "pole_id": job.pole_id,
            "file": job.file,
            "received_at": job.received_at.isoformat(timespec="seconds"),
            "age_seconds": max(0, int((now - job.received_at).total_seconds())),
        }
        for job in jobs[:limit]
    ]
    logger.info("request_id=- daftar job status=%s count=%d", status, len(jobs))
    return JSONResponse(content={"status": status, "count": len(jobs), "jobs": items})
