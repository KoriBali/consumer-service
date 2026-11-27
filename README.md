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

## Daftar job per status
GET /v1/jobs?status=queued|done|failed&limit=N   (token sama dengan endpoint lain)
- count = jumlah seluruh job berstatus itu; jobs = paling banyak N job terlama
  (default 100, maks 1000, limit=0 = jumlah saja).
- received_at (UTC) = waktu file masuk inbox; tidak berubah saat VBA memindahkan file.
- age_seconds dihitung dari jam consumer.
- *.json yang namanya tidak sesuai pola dilewati dan dicatat WARNING di log.

## Perintah
- uv run python -m consumer_service
- uv run python tools/vba_sim.py   # simulasi VBA di Linux
- uv run pytest

## Simulator VBA (tools/vba_sim.py)
Meniru VBA untuk uji end-to-end di Linux: ambil file *.json tertua di inbox,
pindahkan ke done/ (default) atau failed/ dengan nama yang sama, cetak namanya.
JOBS_ROOT dibaca dari --root, env JOBS_ROOT, atau .env. Hanya library standar.

    uv run python tools/vba_sim.py              # file tertua -> done
    uv run python tools/vba_sim.py --fail       # file tertua -> failed
    uv run python tools/vba_sim.py --all        # semua file di inbox -> done
    uv run python tools/vba_sim.py --root /home/dev/inventor-jobs

Setelah dipindah, GET /v1/jobs/{request_id} membalas done atau failed.

## Referensi
PRD — Integrasi Parameter Pole: Producer-Service & Consumer-Service
