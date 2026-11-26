"""Simulator VBA untuk uji end-to-end di Linux (CS-10).

Mengambil file *.json tertua di JOBS_ROOT/inbox lalu memindahkannya ke done
(default) atau failed (--fail) dengan NAMA YANG SAMA, seperti aturan serah
terima VBA. Hanya library standar.

Urutan sumber JOBS_ROOT: --root, variabel lingkungan JOBS_ROOT, lalu .env
di folder kerja.

Contoh:
    uv run python tools/vba_sim.py
    uv run python tools/vba_sim.py --fail
    uv run python tools/vba_sim.py --all --root /home/dev/inventor-jobs
"""

import argparse
import os
import sys
from pathlib import Path

INBOX = "inbox"
DONE = "done"
FAILED = "failed"


def read_env_file(path: Path) -> dict[str, str]:
    """Baca pasangan KEY=VALUE sederhana dari file .env (utf-8)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def resolve_root(cli_root: str | None, env_file: Path) -> Path | None:
    if cli_root:
        return Path(cli_root)
    from_env = os.environ.get("JOBS_ROOT") or read_env_file(env_file).get("JOBS_ROOT")
    return Path(from_env) if from_env else None


def inbox_files_oldest_first(root: Path) -> list[Path]:
    """File *.json di inbox, tertua dulu (mtime, lalu nama)."""
    inbox = root / INBOX
    if not inbox.is_dir():
        return []
    files = [p for p in inbox.iterdir() if p.is_file() and p.suffix == ".json"]
    return sorted(files, key=lambda p: (p.stat().st_mtime_ns, p.name))


def move_job(src: Path, root: Path, target: str) -> Path:
    """Pindahkan file ke done/failed dengan nama yang sama."""
    dest_dir = root / target
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    os.replace(src, dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulasi VBA: inbox -> done/failed.")
    parser.add_argument("--root", help="JOBS_ROOT (default: env JOBS_ROOT atau .env)")
    parser.add_argument(
        "--fail", action="store_true", help="pindahkan ke failed, bukan done"
    )
    parser.add_argument("--all", action="store_true", help="proses semua file di inbox")
    args = parser.parse_args(argv)

    root = resolve_root(args.root, Path(".env"))
    if root is None:
        print(
            "JOBS_ROOT tidak ditemukan: pakai --root, env JOBS_ROOT, atau .env",
            file=sys.stderr,
        )
        return 2

    files = inbox_files_oldest_first(root)
    if not files:
        print(f"inbox kosong: {root / INBOX}")
        return 0

    target = FAILED if args.fail else DONE
    for src in files if args.all else files[:1]:
        move_job(src, root, target)
        print(f"{src.name} -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
