# Architecture

## High-level

`tiktok-archive` is a local pipeline with three boundaries:

1. **Inputs**: TikTok URLs (one at a time or via creator-sync) and TikTok data exports (ZIP).
2. **Processing**: download → transcribe → tag → optional embed.
3. **Outputs**: SQLite metadata, durable transcript JSON files (local + optional R2), web UI for browsing.

No video data ever leaves the machine. Optional R2 mirroring covers transcripts (text) and DB backups only.

## Component map

```
src/tiktok_archive/
├── cli.py                 entry point: argparse with subcommands
├── config.py              env-driven settings, .env loader, paths
├── ingest/
│   ├── schemas.py         SQLAlchemy models (Video, Creator, Job, Tag, …)
│   ├── parser.py          TikTok export → Video rows
│   ├── urls.py            URL normalization, ID/handle extraction
│   └── downloader.py      yt-dlp wrapper with rate limit + global throttle
├── process/
│   ├── analyze.py         orchestrator: download→transcribe→tag for one URL
│   ├── transcribe.py      Whisper backends: mlx-whisper + faster-whisper
│   ├── tag.py             Ollama-based summary/key-points/topics/intent
│   └── qa.py              transcript-grounded Q&A
├── queue/
│   ├── __init__.py        enqueue, claim_next_job, complete_job, fail_job
│   └── worker.py          worker loop with ThreadPoolExecutor
├── storage/__init__.py    LocalStorage, R2Storage, MirroredStorage
├── sync/__init__.py       creator add/list/sync, import-from-export
├── index/embed.py         (Phase 5 stub) ChromaDB embeddings
├── webapp/
│   ├── app.py             Flask routes
│   ├── templates/         Jinja2 templates
│   └── static/            CSS + JS
└── query/                 (reserved for higher-level queries)
```

## Data flow

### Single-video analyze

```
URL ──┐
      ▼
  get_or_create_video (videos table)
      │
      ▼
  yt-dlp → scratch/{video_id}.mp4
      │
      ▼
  ffmpeg extract → wav (tempdir)
      │
      ▼
  Whisper → transcript text
      │
      ▼
  videos.transcript ← text;  delete scratch/{video_id}.mp4
      │
      ▼
  Ollama (tag prompt) → summary, key_points, topics, intent
      │
      ▼
  videos.* ← tags;  storage.put("transcripts/{id}.json", payload)
      │
      ▼
  (optional) R2 mirror writes transcripts/{id}.json to bucket
```

### Creator sync

```
creators.handle ──┐
                  ▼
            yt-dlp profile crawl (flat-playlist, fast)
                  │
                  ▼
        For each entry not seen:
          INSERT INTO videos (source='creator-sync', ...)
          INSERT INTO jobs (kind='download', video_id=...)
                  │
                  ▼
            creators.last_seen_video_id ← first entry id
                  │
                  ▼
              Worker drains the queue
              (single-video analyze for each)
```

## Job queue invariants

- A job is in exactly one of `pending`, `running`, `done`, `failed` at any time.
- Claim is atomic via `UPDATE ... WHERE id=? AND status='pending'`. If `rowcount = 0`, another worker won the race; loop and try the next.
- Stuck jobs (status=running, locked_at older than `JOB_LOCK_TIMEOUT_SEC`) are reset to pending on worker startup.
- Backoff: attempt N is rescheduled `BACKOFF_BASE_SEC × FACTOR^(N-1)` seconds out. Default: 30s, 90s, 270s.
- After `JOB_MAX_ATTEMPTS` failures, the job is permanently `failed` and surfaces in `tiktok-archive stats`.

## Storage layout

```
data/
├── tiktok.db              SQLite (WAL mode, busy_timeout=5000)
├── scratch/               .mp4 files, deleted after transcribe
├── transcripts/           durable transcript JSON, one per video
│   └── transcripts/{video_id}.json
├── db-backups/            local snapshots of tiktok.db
├── chroma/                ChromaDB persistence (Phase 5)
└── logs/
```

R2 layout (when enabled) mirrors the local structure:

```
{bucket}/
├── transcripts/{video_id}.json
└── db-backups/tiktok-{YYYYMMDD-HHMMSS}.db
```

## SQLite tuning

We set the following pragmas on every connection:

| Pragma           | Value   | Why                                        |
| ---------------- | ------- | ------------------------------------------ |
| `journal_mode`   | `WAL`   | Concurrent reader + writer (web UI + worker) |
| `synchronous`    | `NORMAL`| Faster commits, durability still very good for our workload |
| `busy_timeout`   | `5000`  | Wait up to 5s for locks before erroring  |
| `foreign_keys`   | `ON`    | Enforce FK constraints (off by default in SQLite) |

## Cross-platform notes

- Apple Silicon: `mlx-whisper` runs on Metal via the MLX framework. It's much faster and more memory-efficient than torch on M-series.
- NVIDIA: `faster-whisper` (CTranslate2) on CUDA. The same code paths work; `_backend()` picks based on what's importable.
- Linux/CPU: `faster-whisper` falls back to int8 on CPU. Slow but works for testing.

## Cross-machine sync (future)

Phase 1 doc described shared SQLite via Syncthing across the Mac and the 4070 PC. With the queue, the model gets cleaner: the heavier machine runs the worker; both machines share the DB and the transcripts directory. The web UI runs on either side and reads the same data.
