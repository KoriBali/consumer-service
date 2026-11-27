import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from consumer_service import storage
from consumer_service.config import Settings
from consumer_service.main import create_app

TOKEN = "s3cret-token-XYZ-987"
MAX_BODY = 1024


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        jobs_root=tmp_path / "jobs",
        consumer_token=TOKEN,
        max_body_bytes=MAX_BODY,
        log_dir=tmp_path / "logs",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


def headers(request_id: str | None = "req-1", **extra: str) -> dict[str, str]:
    h = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    if request_id is not None:
        h["X-Request-ID"] = request_id
    h.update(extra)
    return h


def auth_headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def inbox(settings: Settings) -> Path:
    return settings.jobs_root / "inbox"


def inbox_files(settings: Settings) -> list[str]:
    return sorted(p.name for p in inbox(settings).iterdir())


# --- startup ---------------------------------------------------------------


def test_lifespan_creates_dirs(client: TestClient, settings: Settings) -> None:
    for name in ("inbox", "done", "failed", ".tmp"):
        assert (settings.jobs_root / name).is_dir()


# --- POST: sukses dan duplikat ---------------------------------------------


def test_post_201_then_duplicate(client: TestClient, settings: Settings) -> None:
    r1 = client.post("/v1/jobs", content=b'{"a": 1}', headers=headers())
    assert r1.status_code == 201
    assert r1.json() == {
        "request_id": "req-1",
        "status": "queued",
        "file": "req-1.json",
    }

    r2 = client.post("/v1/jobs", content=b'{"a": 2}', headers=headers())
    assert r2.status_code == 200
    assert r2.json() == {
        "request_id": "req-1",
        "status": "duplicate",
        "location": "inbox",
    }
    assert inbox_files(settings) == ["req-1.json"]
    assert (inbox(settings) / "req-1.json").read_bytes() == b'{"a": 1}'


@pytest.mark.parametrize("folder", ["done", "failed"])
def test_duplicate_after_move(
    client: TestClient, settings: Settings, folder: str
) -> None:
    h = headers(**{"X-Pole-ID": "P_01"})
    assert client.post("/v1/jobs", content=b"{}", headers=h).status_code == 201
    src = inbox(settings) / "P_01_req-1.json"
    src.rename(settings.jobs_root / folder / src.name)

    r = client.post("/v1/jobs", content=b"{}", headers=h)
    assert r.status_code == 200
    assert r.json()["location"] == folder
    assert inbox_files(settings) == []


# --- POST: penolakan tanpa membuat file --------------------------------------


def test_401_without_token(client: TestClient, settings: Settings) -> None:
    h = headers()
    del h["Authorization"]
    r = client.post("/v1/jobs", content=b"{}", headers=h)
    assert r.status_code == 401
    assert "error" in r.json()
    assert inbox_files(settings) == []


@pytest.mark.parametrize(
    "auth", ["Bearer wrong-token", f"Basic {TOKEN}", TOKEN, "Bearer", "Bearer "]
)
def test_401_wrong_token(client: TestClient, settings: Settings, auth: str) -> None:
    r = client.post("/v1/jobs", content=b"{}", headers=headers(Authorization=auth))
    assert r.status_code == 401
    assert inbox_files(settings) == []


def test_401_checked_before_400(client: TestClient, settings: Settings) -> None:
    h = {"Content-Type": "text/plain", "X-Request-ID": "bad_id"}
    r = client.post("/v1/jobs", content=b"not json", headers=h)
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("body", "extra"),
    [
        (b"not json", {}),
        (b'{"a": 1', {}),
        (b"", {}),
        (b'{"a": "\xff\xfe"}', {}),  # bukan UTF-8
        ('{"a": 1}'.encode("utf-16"), {}),  # JSON valid tapi bukan UTF-8
        (b"{}", {"Content-Type": "text/plain"}),
        (b"{}", {"Content-Type": "application/x-www-form-urlencoded"}),
        (b"{}", {"X-Request-ID": ""}),
        (b"{}", {"X-Request-ID": "a_b"}),
        (b"{}", {"X-Request-ID": "a" * 65}),
        (b"{}", {"X-Request-ID": "a.b"}),
        (b"{}", {"X-Pole-ID": "P.01"}),
        (b"{}", {"X-Pole-ID": "p" * 65}),
    ],
)
def test_400_without_file(
    client: TestClient, settings: Settings, body: bytes, extra: dict[str, str]
) -> None:
    r = client.post("/v1/jobs", content=body, headers=headers(**extra))
    assert r.status_code == 400
    assert "error" in r.json()
    assert inbox_files(settings) == []


def test_400_missing_request_id(client: TestClient, settings: Settings) -> None:
    r = client.post("/v1/jobs", content=b"{}", headers=headers(request_id=None))
    assert r.status_code == 400
    assert inbox_files(settings) == []


def test_413_without_file(client: TestClient, settings: Settings) -> None:
    body = b'{"x": "' + b"a" * MAX_BODY + b'"}'
    r = client.post("/v1/jobs", content=body, headers=headers())
    assert r.status_code == 413
    assert "error" in r.json()
    assert inbox_files(settings) == []


async def _call_post_raw(
    settings: Settings, content_length: int, body: bytes
) -> tuple[int, bool]:
    """Panggil app lewat ASGI mentah; kembalikan (status, apakah body dibaca)."""
    app = create_app(settings)
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers().items()
    ]
    raw_headers.append((b"content-length", str(content_length).encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/jobs",
        "raw_path": b"/v1/jobs",
        "root_path": "",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 1234),
        "server": ("t", 80),
    }
    body_read = False
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal body_read
        body_read = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return int(start["status"]), body_read  # type: ignore[call-overload]


@pytest.mark.asyncio
async def test_413_from_content_length_before_body_read(settings: Settings) -> None:
    storage.ensure_dirs(settings.jobs_root)
    status, body_read = await _call_post_raw(settings, MAX_BODY + 1, b"{}")
    assert status == 413
    assert body_read is False
    assert inbox_files(settings) == []


@pytest.mark.asyncio
async def test_content_length_at_limit_reads_body(settings: Settings) -> None:
    storage.ensure_dirs(settings.jobs_root)
    status, body_read = await _call_post_raw(settings, 2, b"{}")
    assert status == 201
    assert body_read is True


def test_body_exactly_at_limit_accepted(client: TestClient) -> None:
    body = b'{"x": "' + b"a" * (MAX_BODY - 9) + b'"}'
    assert len(body) == MAX_BODY
    assert client.post("/v1/jobs", content=body, headers=headers()).status_code == 201


def test_content_type_with_charset_accepted(client: TestClient) -> None:
    h = headers(**{"Content-Type": "application/json; charset=utf-8"})
    assert client.post("/v1/jobs", content=b"{}", headers=h).status_code == 201


def test_500_when_write_fails(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(root: Path, filename: str, body: bytes) -> Path:
        raise OSError("disk penuh")

    monkeypatch.setattr(storage, "write_job_atomic", boom)
    r = client.post("/v1/jobs", content=b"{}", headers=headers())
    assert r.status_code == 500
    assert "error" in r.json()
    assert inbox_files(settings) == []


# --- POST: isi dan nama file -------------------------------------------------


def test_body_stored_byte_for_byte(client: TestClient, settings: Settings) -> None:
    body = '{\r\n  "nama": "ポール Ø",\n  "x":1.50,"y" : [ ]}\n'.encode()
    r = client.post("/v1/jobs", content=body, headers=headers())
    assert r.status_code == 201
    stored = (inbox(settings) / r.json()["file"]).read_bytes()
    assert hashlib.sha256(stored).hexdigest() == hashlib.sha256(body).hexdigest()


def test_filename_without_pole(client: TestClient, settings: Settings) -> None:
    r = client.post("/v1/jobs", content=b"{}", headers=headers("ABC-123"))
    assert r.json()["file"] == "abc-123.json"
    assert inbox_files(settings) == ["abc-123.json"]


def test_filename_with_pole(client: TestClient, settings: Settings) -> None:
    h = headers("ABC-123", **{"X-Pole-ID": "Pole_7"})
    r = client.post("/v1/jobs", content=b"{}", headers=h)
    assert r.json()["file"] == "Pole_7_abc-123.json"
    assert inbox_files(settings) == ["Pole_7_abc-123.json"]


def test_empty_pole_id_ignored(client: TestClient, settings: Settings) -> None:
    h = headers(**{"X-Pole-ID": ""})
    r = client.post("/v1/jobs", content=b"{}", headers=h)
    assert r.json()["file"] == "req-1.json"


def test_post_response_request_id_normalized(
    client: TestClient, settings: Settings
) -> None:
    r1 = client.post("/v1/jobs", content=b"{}", headers=headers("Smoke-1"))
    assert r1.status_code == 201
    assert r1.json()["request_id"] == "smoke-1"
    assert r1.json()["file"] == "smoke-1.json"

    r2 = client.post("/v1/jobs", content=b"{}", headers=headers("SMOKE-1"))
    assert r2.status_code == 200
    assert r2.json()["request_id"] == "smoke-1"

    r3 = client.get("/v1/jobs/SMOKE-1", headers=auth_headers())
    assert r3.status_code == 200
    assert r3.json() == {"request_id": "smoke-1", "status": "queued"}


# --- POST: bersamaan -------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_same_request_id(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.ensure_dirs(settings.jobs_root)  # ASGITransport tidak menjalankan lifespan
    real_write = storage.write_job_atomic

    def slow_write(root: Path, filename: str, body: bytes) -> Path:
        time.sleep(0.05)  # perlebar jendela balapan
        return real_write(root, filename, body)

    monkeypatch.setattr(storage, "write_job_atomic", slow_write)

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        responses = await asyncio.gather(
            ac.post("/v1/jobs", content=b'{"n": 1}', headers=headers()),
            ac.post("/v1/jobs", content=b'{"n": 2}', headers=headers()),
        )

    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 201]
    dup = next(r for r in responses if r.status_code == 200)
    assert dup.json()["status"] == "duplicate"
    assert inbox_files(settings) == ["req-1.json"]


# --- GET /v1/jobs/{id} -----------------------------------------------------


@pytest.mark.parametrize(
    ("folder", "status"),
    [("inbox", "queued"), ("done", "done"), ("failed", "failed")],
)
def test_get_status(
    client: TestClient, settings: Settings, folder: str, status: str
) -> None:
    (settings.jobs_root / folder / "P1_req-9.json").write_bytes(b"{}")
    r = client.get("/v1/jobs/req-9", headers=auth_headers())
    assert r.status_code == 200
    assert r.json() == {"request_id": "req-9", "status": status}


def test_get_status_unknown(client: TestClient) -> None:
    r = client.get("/v1/jobs/req-404", headers=auth_headers())
    assert r.status_code == 200
    assert r.json() == {"request_id": "req-404", "status": "unknown"}


@pytest.mark.parametrize("bad", ["a_b", "a" * 65, "a.b"])
def test_get_status_invalid_id(client: TestClient, bad: str) -> None:
    r = client.get(f"/v1/jobs/{bad}", headers=auth_headers())
    assert r.status_code == 400
    assert "error" in r.json()


@pytest.mark.parametrize(
    "auth",
    [None, "Bearer wrong-token", f"Basic {TOKEN}", TOKEN, "Bearer", "Bearer "],
)
def test_get_status_401(
    client: TestClient, settings: Settings, auth: str | None
) -> None:
    (settings.jobs_root / "inbox" / "req-9.json").write_bytes(b"{}")
    h = {} if auth is None else {"Authorization": auth}
    r = client.get("/v1/jobs/req-9", headers=h)
    assert r.status_code == 401
    assert "error" in r.json()
    assert "status" not in r.json()


def test_get_status_401_checked_before_400(client: TestClient) -> None:
    r = client.get("/v1/jobs/a_b")
    assert r.status_code == 401


# --- /health -----------------------------------------------------------------


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "inbox_writable": True}


def test_health_does_not_leave_files(client: TestClient, settings: Settings) -> None:
    client.get("/health")
    assert inbox_files(settings) == []


# --- logging -----------------------------------------------------------------


def test_log_contains_request_id_and_never_token(
    client: TestClient, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client.post("/v1/jobs", content=b"{}", headers=headers("log-ok"))
    client.post("/v1/jobs", content=b"{}", headers=headers("log-ok"))
    client.post("/v1/jobs", content=b"nope", headers=headers("log-bad"))
    client.post(
        "/v1/jobs",
        content=b"{}",
        headers=headers("log-unauth", Authorization=f"Bearer {TOKEN}-wrong"),
    )
    client.get("/v1/jobs/log-ok", headers=auth_headers())

    for rid in ("log-ok", "log-bad", "log-unauth"):
        lines = [r.getMessage() for r in caplog.records if rid in r.getMessage()]
        assert lines, f"tidak ada baris log untuk {rid}"

    assert TOKEN not in caplog.text
    log_files = list(settings.log_dir.glob("*.log"))
    assert log_files, "file log tidak dibuat di LOG_DIR"
    file_text = "".join(p.read_text(encoding="utf-8") for p in log_files)
    assert "log-ok" in file_text
    assert TOKEN not in file_text


def test_create_app_does_not_duplicate_log_handlers(settings: Settings) -> None:
    create_app(settings)
    first = len(logging.getLogger("consumer_service").handlers)
    create_app(settings)
    assert len(logging.getLogger("consumer_service").handlers) == first


def test_response_json_shape_is_json(client: TestClient) -> None:
    r = client.post("/v1/jobs", content=b"{}", headers=headers())
    assert r.headers["content-type"].startswith("application/json")
    json.loads(r.content)


# --- GET /v1/jobs (daftar) ---------------------------------------------------


def _post(client: TestClient, request_id: str, pole_id: str | None = None) -> None:
    extra = {"X-Pole-ID": pole_id} if pole_id else {}
    r = client.post("/v1/jobs", content=b"{}", headers=headers(request_id, **extra))
    assert r.status_code == 201


def test_list_queued_after_post(client: TestClient) -> None:
    _post(client, "REQ-1", "P-1")

    r = client.get("/v1/jobs", params={"status": "queued"}, headers=auth_headers())

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "queued"
    assert data["count"] == 1
    (job,) = data["jobs"]
    assert job["request_id"] == "req-1"
    assert job["pole_id"] == "P-1"
    assert job["file"] == "P-1_req-1.json"
    assert job["received_at"].endswith("+00:00")
    assert 0 <= job["age_seconds"] < 60


def test_list_done_after_move(client: TestClient, settings: Settings) -> None:
    _post(client, "req-1")
    (inbox(settings) / "req-1.json").rename(settings.jobs_root / "done" / "req-1.json")

    queued = client.get("/v1/jobs?status=queued", headers=auth_headers()).json()
    done = client.get("/v1/jobs?status=done", headers=auth_headers()).json()

    assert queued["count"] == 0
    assert [j["request_id"] for j in done["jobs"]] == ["req-1"]


def test_list_limit_keeps_total_count(client: TestClient) -> None:
    for i in range(3):
        _post(client, f"req-{i}")

    two = client.get("/v1/jobs?status=queued&limit=2", headers=auth_headers()).json()
    zero = client.get("/v1/jobs?status=queued&limit=0", headers=auth_headers()).json()

    assert two["count"] == 3 and len(two["jobs"]) == 2
    assert zero["count"] == 3 and zero["jobs"] == []


def test_list_401(client: TestClient) -> None:
    r = client.get("/v1/jobs?status=queued", headers=auth_headers("salah"))
    assert r.status_code == 401


def test_list_401_checked_before_400(client: TestClient) -> None:
    r = client.get("/v1/jobs?status=bukan", headers=auth_headers("salah"))
    assert r.status_code == 401


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?status=",
        "?status=unknown",
        "?status=queued&limit=-1",
        "?status=queued&limit=abc",
        "?status=queued&limit=1001",
    ],
)
def test_list_400(client: TestClient, query: str) -> None:
    r = client.get(f"/v1/jobs{query}", headers=auth_headers())
    assert r.status_code == 400
    assert "error" in r.json()


def test_list_logs_skipped_name(
    client: TestClient, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    (settings.jobs_root / "failed" / "hasil model.json").write_bytes(b"{}")

    with caplog.at_level(logging.WARNING, logger="consumer_service"):
        r = client.get("/v1/jobs?status=failed", headers=auth_headers())

    assert r.json()["count"] == 0
    assert "hasil model.json" in caplog.text
    assert all("request_id=" in rec.getMessage() for rec in caplog.records)


def test_single_status_route_still_works(client: TestClient) -> None:
    _post(client, "req-1")
    r = client.get("/v1/jobs/req-1", headers=auth_headers())
    assert r.json() == {"request_id": "req-1", "status": "queued"}
