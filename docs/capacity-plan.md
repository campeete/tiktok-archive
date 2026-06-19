# Capacity plan

## Disk

The unit of cost is the transcript JSON, not the video.

| Asset                    | Size per item        | At 50K items   |
| ------------------------ | -------------------- | -------------- |
| `data/transcripts/*.json` | ~10 KB              | 500 MB         |
| `data/tiktok.db`         | ~5-10 KB per video  | ~500 MB        |
| `data/scratch/*.mp4`     | ~10 MB (transient)  | bounded by concurrency |
| `data/db-backups/*.db`   | size of `tiktok.db` | rotated         |

**Total resident disk for a 50K-video archive: ~1 GB.**

Compare to keeping videos: 50K × 10 MB = 500 GB. The transcribe-and-discard policy is the difference between "fits on a laptop" and "needs an external drive."

### Scratch directory bound

At any moment, scratch holds at most:
- `WORKER_DOWNLOAD_CONCURRENCY × max-video-size` ≈ 1 × 50 MB = 50 MB

With default concurrency, scratch never exceeds ~100 MB. Set `TT_SCRATCH_DIR` to `/tmp` or a separate drive if you want.

### DB backup retention

`tiktok-archive backup-db` writes a copy each call. The default retention policy is 30 days; cleanup is manual or via `scripts/backup-and-cleanup.sh`. At 500 MB per backup × 30 days, the local backup directory peaks at ~15 GB. R2 backups follow the same retention.

## Throughput

### Single-machine (M-series MacBook Pro)

| Stage           | Per-video time | Bottleneck       |
| --------------- | -------------- | ---------------- |
| yt-dlp download | 5-15 s         | TikTok throttle (>10s sleep) |
| ffmpeg extract  | 1-3 s          | Disk + CPU       |
| Whisper (medium)| 8-30 s         | Mac GPU (Metal)  |
| Ollama tag      | 10-25 s        | Mac GPU (qwen2.5:7b) |
| Storage put     | <1 s local     |                  |
| **Total**       | **~30-90 s/video** |              |

At 60 s average, a single Mac drains ~60 videos/hour.

### Cross-machine (4070 PC + Mac UI)

The 4070 transcribes ~5× faster on faster-whisper than M-series on mlx-whisper. Throughput scales accordingly; expect 200-300 videos/hour on the PC. The Mac runs the UI without any inference load.

### Rate limit ceiling

TikTok rate limits will engage well before you saturate hardware. With `YTDLP_SLEEP_INTERVAL=3` (default), you're capped at roughly 1200 downloads/hour theoretical, but in practice 200-400/hour is more realistic before triggering a 429.

The ceiling for a 50K-video archive is therefore **~125 hours of continuous syncing** — about a week. This is the number to anchor expectations on. Don't expect to ingest a multi-year archive overnight.

### When you hit a 429

The global throttle pauses everything for `RATE_LIMIT_PAUSE_SEC` (default 1 hour). After the pause, `WORKER_IDLE_SLEEP_SEC` polling resumes naturally. Don't shorten the pause; banned IP is a much worse outcome.

## Scaling planning

### 500 videos (initial test)

- Disk: ~5 MB transcripts.
- Time: ~10 hours of worker on Mac.
- One sync run per creator catches everything.
- No special setup required.

### 5,000 videos

- Disk: ~50 MB transcripts.
- Time: ~80 hours of worker on Mac, or ~20 hours on the 4070 PC.
- Recommend the 4070 PC for the bulk pull; Mac for daily incremental.
- Set up nightly worker via launchd/systemd.

### 50,000 videos (the design target)

- Disk: ~500 MB transcripts, ~500 MB DB.
- Time: ~125 hours of worker, distributed across ~weeks.
- R2 mirror strongly recommended at this scale.
- ChromaDB (Phase 5) becomes the only practical way to search. SQL `LIKE` queries get slow past ~10K rows.
- Plan for `tiktok-archive backup-db` nightly to R2.

## Memory

| Component           | Working set         | Peak memory        |
| ------------------- | ------------------- | ------------------ |
| Whisper medium (mlx)| ~1.5 GB             | ~3 GB during decode |
| Whisper medium (CUDA)| ~1.5 GB VRAM       | ~2.5 GB VRAM       |
| Ollama qwen2.5:7b   | ~5 GB RAM           | ~6 GB during gen   |
| Flask + Python      | ~150 MB             | ~250 MB            |
| Worker + threads    | ~100 MB             | ~300 MB during job |
| **Total**           | **~7 GB**           | **~10 GB peak**    |

A 16 GB Mac is comfortable; an 8 GB Mac will swap. The 4070 PC has 32 GB; no concern.

## Cost (R2)

R2 free tier:
- 10 GB storage (we use ~500 MB)
- 1M Class A operations/month (writes; we use ~50K/month at full sync)
- 10M Class B operations/month (reads; trivial)
- Zero egress

We never come close to free-tier limits at 50K videos. R2 cost: $0.

If we expanded scope to 5M videos (every video on TikTok by your followed creators): 50 GB transcripts, $0.75/mo. Still fine.

## When to revisit

| Trigger                                | Revisit                              |
| -------------------------------------- | ------------------------------------ |
| >100K videos                           | Migrate to Postgres? Probably not, but evaluate. |
| Multi-user                             | Auth, multi-tenancy                  |
| Multi-machine workers                   | Move queue to Redis/Postgres        |
| LIKE queries >1s                        | Move search to ChromaDB (Phase 5)   |
| `data/transcripts/` >10K files in a directory | Shard by `video_id // 1000`     |
