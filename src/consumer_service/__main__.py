import uvicorn

from consumer_service.config import Settings
from consumer_service.main import create_app


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # dibaca dari env / .env
    # Satu worker: lock duplikat (CS-5) hanya berlaku dalam satu proses.
    # loop="asyncio": tanpa uvloop agar sama perilakunya di Windows.
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        loop="asyncio",
    )


if __name__ == "__main__":
    main()
