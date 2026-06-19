# Job queue and worker

## Why a queue

Phase 1.5 was synchronous: paste a URL, wait 60 seconds, get a result. That's fine for one-off use. But once you're syncing dozens of creators with hundreds of new videos, you don't want every analysis blocking the CLI or the web UI. The queue decouples "I'd like this analyzed" from "actually doing the work."

## Schema

```
CREATE TABLE jobs (
  id              INTEGER PRIMARY KEY,
  kind            TEXT NOT NULL,         -- download, transcribe, tag, embed, sync-creator
  video_id        INTEGER REFERENCES videos(id),
  creator_id      INTEGER REFERENCES creators(id),
  status          TEXT NOT NULL,         -- pending, running, done, failed
  payload         TEXT,                  -- JSON, kind-specific
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  locked_at       TIMESTAMP,
  locked_by       TEXT,
  scheduled_for   TIMESTAMP NOT NULL,
  started_at      TIMESTAMP,
  finished_at     TIMESTAMP,
  duration_sec    REAL,
  created_at      TIMESTAMP NOT NULL,
  updated_at      TIMESTAMP NOT NULL
);
CREATE INDEX ix_jobs_pending ON jobs(status, kind, scheduled_for);
```

## Atomic claim

Two workers must never grab the same job. The claim is:

```sql
-- 1) find the next eligible job
SELECT id FROM jobs
WHERE status = 'pending' AND scheduled_for <= now()
ORDER BY scheduled_for ASC, id ASC
LIMIT 1;

-- 2) atomically claim it
UPDATE jobs
SET status = 'running', locked_at = now(), locked_by = ?, started_at = now(), attempts = attempts + 1
WHERE id = ? AND status = 'pending';
-- if rowcount = 0, another worker beat us: retry from step 1
```

This pattern works on SQLite because every UPDATE is serialized by the database lock.

## Backoff

Failed jobs are rescheduled with exponential backoff:

```
delay = JOB_BACKOFF_BASE_SEC × JOB_BACKOFF_FACTOR^(attempts - 1)
```

Defaults (30s base, factor 3): **30s → 90s → 270s → permanent failure** after 3 attempts.

## Stuck jobs

If a worker crashes mid-job, its job stays in `running` forever unless we recover it. On worker startup, we run:

```python
UPDATE jobs SET status='pending', locked_at=NULL, locked_by=NULL
WHERE status='running' AND locked_at < now() - JOB_LOCK_TIMEOUT_SEC;
```

`JOB_LOCK_TIMEOUT_SEC` defaults to 30 minutes — well above any expected job duration.

## Concurrency

The worker uses a single `ThreadPoolExecutor`. Per-stage limits are enforced inside the dispatcher:

| Stage      | Limit                                | Why                              |
| ---------- | ------------------------------------ | -------------------------------- |
| download   | `WORKER_DOWNLOAD_CONCURRENCY = 1`    | TikTok rate limits               |
| transcribe | `WORKER_TRANSCRIBE_CONCURRENCY = 1`  | Whisper is single-threaded; multiple instances thrash GPU |
| tag        | `WORKER_TAG_CONCURRENCY = 2`         | Ollama can serve 2 concurrent generations on most setups |
| embed      | `WORKER_EMBED_CONCURRENCY = 2`       | Same as tag                      |

Note: in the current implementation, `download → transcribe → tag` happens inside a single `download` job because the analyze pipeline is atomic. If you want to split them later, the kinds are reserved.

## Running the worker

### One-shot drain (cron-friendly)

```bash
tiktok-archive worker --once
# or with a cap:
tiktok-archive worker --once --max-jobs 50
```

### Continuous

```bash
tiktok-archive worker
```

Sends SIGTERM/SIGINT — drains in-flight jobs cleanly.

### Daemonize on macOS (launchd)

See `scripts/com.tiktok-archive.worker.plist`. Drop it in `~/Library/LaunchAgents/` and:

```bash
launchctl load ~/Library/LaunchAgents/com.tiktok-archive.worker.plist
launchctl start com.tiktok-archive.worker
```

## Inspecting the queue

```bash
tiktok-archive stats
```

Or the web UI: http://127.0.0.1:5050/queue (auto-refreshes every 5s).

## Manual interventions

### Drain failed jobs back to pending

```sql
sqlite3 data/tiktok.db "UPDATE jobs SET status='pending', attempts=0, last_error=NULL WHERE status='failed';"
```

### Cancel pending jobs older than 7 days

```sql
sqlite3 data/tiktok.db "DELETE FROM jobs WHERE status='pending' AND created_at < datetime('now', '-7 days');"
```

### Clear the entire queue (videos table preserved)

```sql
sqlite3 data/tiktok.db "DELETE FROM jobs;"
```
