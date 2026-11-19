import hashlib
import os
from pathlib import Path

import pytest

from consumer_service import storage

FOLDERS = ("inbox", "done", "failed", ".tmp")


def test_ensure_dirs_creates_folders(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    storage.ensure_dirs(root)
    for name in FOLDERS:
        assert (root / name).is_dir()
    storage.ensure_dirs(root)  # idempoten


def test_ensure_dirs_removes_leftover_part(tmp_path: Path) -> None:
    tmp = tmp_path / ".tmp"
    tmp.mkdir()
    leftover = tmp / "abc.json.part"
    leftover.write_bytes(b"{")
    other = tmp / "keep.txt"
    other.write_bytes(b"x")

    storage.ensure_dirs(tmp_path)

    assert not leftover.exists()
    assert other.exists()


def test_build_filename_without_pole() -> None:
    assert storage.build_filename("abc-123") == "abc-123.json"
    assert storage.build_filename("abc-123", "") == "abc-123.json"


def test_build_filename_with_pole() -> None:
    assert storage.build_filename("abc-1", "P-01") == "P-01_abc-1.json"


def test_build_filename_lowercases_request_id() -> None:
    assert storage.build_filename("ABC-Def") == "abc-def.json"


def test_build_filename_accepts_64_chars() -> None:
    rid = "a" * 64
    assert storage.build_filename(rid) == f"{rid}.json"


INVALID = ["", "a" * 65, "../x", "a.b", "a b", "a:b", "a\\b", "a/b", "a*b"]


@pytest.mark.parametrize("bad", INVALID)
def test_build_filename_rejects_invalid_request_id(bad: str) -> None:
    with pytest.raises(ValueError):
        storage.build_filename(bad)


@pytest.mark.parametrize("bad", ["a_b", "_ab", "ab_", "_"])
def test_request_id_with_underscore_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        storage.build_filename(bad)
    with pytest.raises(ValueError):
        storage.find_existing(tmp_path, bad)
    with pytest.raises(ValueError):
        storage.get_status(tmp_path, bad)


def test_pole_id_with_underscore_allowed() -> None:
    assert storage.build_filename("abc-1", "P_01") == "P_01_abc-1.json"


def test_underscore_request_id_cannot_shadow_other_id(tmp_path: Path) -> None:
    storage.ensure_dirs(tmp_path)
    with pytest.raises(ValueError):
        name = storage.build_filename("a_b")  # sebelum fix: "a_b.json"
        storage.write_job_atomic(tmp_path, name, b"{}")
    assert storage.find_existing(tmp_path, "b") is None


@pytest.mark.parametrize("bad", [b for b in INVALID if b])
def test_build_filename_rejects_invalid_pole_id(bad: str) -> None:
    with pytest.raises(ValueError):
        storage.build_filename("abc", bad)


def test_write_job_atomic_is_byte_identical(tmp_path: Path) -> None:
    storage.ensure_dirs(tmp_path)
    body = '{"nama": "ポール",\r\n "x": 1}\n'.encode() + b"\n\r\n"

    final = storage.write_job_atomic(tmp_path, "abc.json", body)

    assert final == tmp_path / "inbox" / "abc.json"
    expected = hashlib.sha256(body).hexdigest()
    assert hashlib.sha256(final.read_bytes()).hexdigest() == expected


def test_inbox_never_contains_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.ensure_dirs(tmp_path)
    inbox = tmp_path / "inbox"
    real_replace = os.replace
    seen: list[tuple[str, str]] = []

    def checking_replace(src, dst) -> None:
        seen.append((str(src), str(dst)))
        assert Path(src).parent == tmp_path / ".tmp"
        assert Path(src).name == "abc.json.part"
        assert Path(src).exists()
        assert list(inbox.iterdir()) == []
        real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", checking_replace)

    storage.write_job_atomic(tmp_path, "abc.json", b"{}")

    assert len(seen) == 1
    assert list(inbox.glob("*.part")) == []
    assert [p.name for p in inbox.iterdir()] == ["abc.json"]
    assert list((tmp_path / ".tmp").iterdir()) == []


@pytest.mark.parametrize("folder", ["inbox", "done", "failed"])
@pytest.mark.parametrize("filename", ["req-1.json", "P01_req-1.json"])
def test_find_existing_detects_duplicate(
    tmp_path: Path, folder: str, filename: str
) -> None:
    storage.ensure_dirs(tmp_path)
    (tmp_path / folder / filename).write_bytes(b"{}")

    assert storage.find_existing(tmp_path, "req-1") == folder
    assert storage.find_existing(tmp_path, "REQ-1") == folder
    assert storage.find_existing(tmp_path, "req-2") is None


def test_find_existing_none_when_empty(tmp_path: Path) -> None:
    storage.ensure_dirs(tmp_path)
    assert storage.find_existing(tmp_path, "abc") is None


def test_find_existing_rejects_invalid_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        storage.find_existing(tmp_path, "*")


@pytest.mark.parametrize(
    ("folder", "status"),
    [("inbox", "queued"), ("done", "done"), ("failed", "failed")],
)
def test_get_status(tmp_path: Path, folder: str, status: str) -> None:
    storage.ensure_dirs(tmp_path)
    (tmp_path / folder / "P1_abc.json").write_bytes(b"{}")
    assert storage.get_status(tmp_path, "abc") == status


def test_get_status_unknown(tmp_path: Path) -> None:
    storage.ensure_dirs(tmp_path)
    assert storage.get_status(tmp_path, "abc") == "unknown"


def test_replace_retries_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.ensure_dirs(tmp_path)
    real_replace = os.replace
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky_replace(src, dst) -> None:
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError("locked")
        real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", flaky_replace)
    monkeypatch.setattr(storage.time, "sleep", sleeps.append)

    final = storage.write_job_atomic(tmp_path, "abc.json", b"{}")

    assert final.read_bytes() == b"{}"
    assert len(calls) == 3
    assert sleeps == [0.1, 0.1]


def test_replace_gives_up_after_five_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.ensure_dirs(tmp_path)
    calls: list[int] = []

    def locked_replace(src, dst) -> None:
        calls.append(1)
        raise PermissionError("locked")

    monkeypatch.setattr(storage.os, "replace", locked_replace)
    monkeypatch.setattr(storage.time, "sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        storage.write_job_atomic(tmp_path, "abc.json", b"{}")

    assert len(calls) == 5
    assert list((tmp_path / "inbox").iterdir()) == []
    assert list((tmp_path / ".tmp").iterdir()) == []
