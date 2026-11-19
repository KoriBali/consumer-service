import tempfile

from fastapi import APIRouter, Request

from consumer_service import storage
from consumer_service.config import Settings

router = APIRouter()


def _inbox_writable(settings: Settings) -> bool:
    # Uji tulis sungguhan: os.access tidak andal untuk ACL di Windows.
    # Nama probe tidak berakhiran .json sehingga tidak pernah dibaca VBA.
    try:
        with tempfile.TemporaryFile(
            dir=settings.jobs_root / storage.INBOX, prefix=".health-", suffix=".probe"
        ) as f:
            f.write(b"ok")
        return True
    except OSError:
        return False


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    return {"status": "ok", "inbox_writable": _inbox_writable(settings)}
