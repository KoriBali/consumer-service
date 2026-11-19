import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from consumer_service import storage
from consumer_service.api import health, jobs
from consumer_service.config import Settings

LOGGER_NAME = "consumer_service"
LOG_FILENAME = "consumer-service.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

logger = logging.getLogger(LOGGER_NAME)


def _configure_logging(log_dir: Path) -> None:
    """Log ke konsol dan ke LOG_DIR/consumer-service.log (utf-8).

    Handler lama milik service ini ditutup dulu agar create_app bisa dipanggil
    berulang (mis. di tes) tanpa baris log ganda.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    for handler in list(logger.handlers):
        if getattr(handler, "_consumer_service", False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT)
    console = logging.StreamHandler()
    file_handler = logging.FileHandler(log_dir / LOG_FILENAME, encoding="utf-8")
    for handler in (console, file_handler):
        handler.setFormatter(formatter)
        handler._consumer_service = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()  # type: ignore[call-arg]  # dibaca dari env / .env
    _configure_logging(settings.log_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        storage.ensure_dirs(settings.jobs_root)
        logger.info("siap, JOBS_ROOT=%s", settings.jobs_root)
        yield

    app = FastAPI(title="consumer-service", lifespan=lifespan)
    app.state.settings = settings
    app.state.jobs_lock = asyncio.Lock()
    app.include_router(jobs.router)
    app.include_router(health.router)
    return app
