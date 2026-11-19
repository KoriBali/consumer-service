# consumer-service

Menerima payload dari producer-service dan menulisnya apa adanya sebagai file JSON
ke JOBS_ROOT/inbox untuk dibaca VBA di Autodesk Inventor.

## Batasan
- Dikembangkan di Linux, BERJALAN DI WINDOWS. Wajib lintas platform:
  pathlib, tulis mode biner, tanpa fcntl/fork/uvloop, encoding utf-8 eksplisit.
- Tanpa database. Duplikat dan status dibaca dari folder inbox/done/failed.
- Tanpa kontrak payload. Cek hanya: token, header, ukuran, JSON valid (UTF-8).
- Jalan dengan SATU worker (lock duplikat hanya berlaku dalam satu proses).

## Alur tulis
.tmp/{nama}.part -> fsync -> os.replace -> inbox/{nama}
nama = {pole_id}_{request_id}.json atau {request_id}.json

## Serah terima dengan VBA
- VBA hanya membaca *.json di inbox.
- VBA memindahkan file ke done/ atau failed/ dengan NAMA YANG SAMA.

## Perintah
- uv run python -m consumer_service
- uv run python tools/vba_sim.py   # simulasi VBA di Linux
- uv run pytest

## Referensi
PRD — Integrasi Parameter Pole: Producer-Service & Consumer-Service
