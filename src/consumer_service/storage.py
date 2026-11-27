"""Penyimpanan job berbasis file system di bawah JOBS_ROOT (tanpa database).

Semua fungsi menerima ``root`` sebagai argumen; modul ini tidak membaca Settings.
"""

import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

INBOX = "inbox"
DONE = "done"
FAILED = "failed"
TMP = ".tmp"
PART_SUFFIX = ".part"

REPLACE_ATTEMPTS = 5
REPLACE_DELAY = 0.1

# request_id tanpa "_" agar `{pole_id}_{request_id}.json` selalu bisa diurai tanpa
# ambigu: "a_b.json" hanya berarti pole "a" + request "b".
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_POLE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_STATUS_BY_FOLDER = {INBOX: "queued", DONE: "done", FAILED: "failed"}
_FOLDER_BY_STATUS = {status: folder for folder, status in _STATUS_BY_FOLDER.items()}
LIST_STATUSES = tuple(_FOLDER_BY_STATUS)
JOB_SUFFIX = ".json"


@dataclass(frozen=True)
class JobEntry:
    """Satu file job di inbox, done, atau failed."""

    request_id: str
    pole_id: str | None
    file: str
    received_at: datetime


def _validate_id(value: str, name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} tidak valid: hanya {pattern.pattern}")
    return value


def _normalize_request_id(request_id: str) -> str:
    return _validate_id(request_id, "request_id", _REQUEST_ID_RE).lower()


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
    return f"{_validate_id(pole_id, 'pole_id', _POLE_ID_RE)}_{rid}.json"


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


def parse_filename(name: str) -> tuple[str, str | None] | None:
    """(request_id, pole_id) dari nama file job, atau None bila tidak sesuai pola.

    Kebalikan build_filename. Dipisah di "_" terakhir: request_id tidak pernah
    memuat "_", sedangkan pole_id boleh.
    """
    if not name.endswith(JOB_SUFFIX):
        return None
    pole_id, sep, request_id = name.removesuffix(JOB_SUFFIX).rpartition("_")
    if not _REQUEST_ID_RE.fullmatch(request_id) or request_id != request_id.lower():
        return None
    if not sep:
        return request_id, None
    if not _POLE_ID_RE.fullmatch(pole_id):
        return None
    return request_id, pole_id


def list_jobs(root: Path, status: str) -> tuple[list[JobEntry], list[str]]:
    """Daftar job berstatus `status`, terlama dulu, plus nama file yang dilewati.

    received_at = waktu modifikasi file, yaitu saat consumer menulisnya ke inbox.
    Memindahkan file (seperti yang dilakukan VBA) tidak mengubah waktu ini.
    File selain *.json diabaikan tanpa dilaporkan (mis. desktop.ini di Windows).
    File yang hilang saat daftar dibaca, karena sedang dipindah VBA, dilewati.
    """
    folder_name = _FOLDER_BY_STATUS.get(status)
    if folder_name is None:
        raise ValueError(f"status tidak valid: pilih {', '.join(LIST_STATUSES)}")
    folder = root / folder_name
    jobs: list[JobEntry] = []
    skipped: list[str] = []
    if not folder.is_dir():
        return jobs, skipped
    for path in folder.iterdir():
        if not path.name.endswith(JOB_SUFFIX):
            continue
        parsed = parse_filename(path.name)
        if parsed is None:
            skipped.append(path.name)
            continue
        try:
            info = os.stat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        request_id, pole_id = parsed
        received_at = datetime.fromtimestamp(info.st_mtime, tz=UTC)
        jobs.append(JobEntry(request_id, pole_id, path.name, received_at))
    jobs.sort(key=lambda job: (job.received_at, job.file))
    return jobs, sorted(skipped)
