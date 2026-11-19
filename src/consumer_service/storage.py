"""Penyimpanan job berbasis file system di bawah JOBS_ROOT (tanpa database).

Semua fungsi menerima ``root`` sebagai argumen; modul ini tidak membaca Settings.
"""

import os
import re
import time
from pathlib import Path

INBOX = "inbox"
DONE = "done"
FAILED = "failed"
TMP = ".tmp"
PART_SUFFIX = ".part"

REPLACE_ATTEMPTS = 5
REPLACE_DELAY = 0.1

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_STATUS_BY_FOLDER = {INBOX: "queued", DONE: "done", FAILED: "failed"}


def _validate_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"{name} tidak valid: hanya [A-Za-z0-9_-], 1-64 karakter")
    return value


def _normalize_request_id(request_id: str) -> str:
    return _validate_id(request_id, "request_id").lower()


def ensure_dirs(root: Path) -> None:
    """Buat inbox, done, failed, .tmp dan hapus sisa *.part di .tmp."""
    for name in (INBOX, DONE, FAILED, TMP):
        (root / name).mkdir(parents=True, exist_ok=True)
    for leftover in (root / TMP).glob(f"*{PART_SUFFIX}"):
        leftover.unlink(missing_ok=True)


def build_filename(request_id: str, pole_id: str | None = None) -> str:
    """`{pole_id}_{request_id}.json` atau `{request_id}.json` bila pole_id kosong."""
    rid = _normalize_request_id(request_id)
    if not pole_id:
        return f"{rid}.json"
    return f"{_validate_id(pole_id, 'pole_id')}_{rid}.json"


def find_existing(root: Path, request_id: str) -> str | None:
    """Kembalikan folder ("inbox", "done", "failed") tempat request_id ada, atau None."""
    rid = _normalize_request_id(request_id)
    for name in (INBOX, DONE, FAILED):
        folder = root / name
        if not folder.is_dir():
            continue
        if (folder / f"{rid}.json").is_file() or any(folder.glob(f"*_{rid}.json")):
            return name
    return None


def _replace_with_retry(src: Path, dst: Path) -> None:
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS:
                raise
            time.sleep(REPLACE_DELAY)


def write_job_atomic(root: Path, filename: str, body: bytes) -> Path:
    """Tulis body byte-per-byte ke .tmp/<nama>.part lalu pindahkan atomik ke inbox."""
    part = root / TMP / f"{filename}{PART_SUFFIX}"
    final = root / INBOX / filename
    try:
        with open(part, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return final


def get_status(root: Path, request_id: str) -> str:
    """Status job berdasarkan lokasi file: queued, done, failed, atau unknown."""
    location = find_existing(root, request_id)
    if location is None:
        return "unknown"
    return _STATUS_BY_FOLDER[location]
