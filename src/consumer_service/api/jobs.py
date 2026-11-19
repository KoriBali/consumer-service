import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from consumer_service import storage
from consumer_service.config import Settings
from consumer_service.security import is_authorized

logger = logging.getLogger("consumer_service.jobs")

router = APIRouter()


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _log_id(value: str | None) -> str:
    """Nilai header untuk log; dipotong dan di-repr karena belum tervalidasi."""
    if value is None:
        return "-"
    return repr(value[:80])


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

    # 3) ukuran
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
    try:
        status = storage.get_status(settings.jobs_root, request_id)
    except ValueError as exc:
        logger.warning("request_id=%s 400 %s", _log_id(request_id), exc)
        return _error(400, str(exc))
    logger.info("request_id=%s status=%s", request_id, status)
    return JSONResponse(content={"request_id": request_id, "status": status})
