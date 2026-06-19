# Creator sync

## Purpose

Periodically pull new videos from a list of registered creators and run them through the analyze pipeline.

## Mental model

You maintain a list of creators in the `creators` table. The first time you sync a creator, we walk back `sync_depth` videos. Every subsequent sync uses `last_seen_video_id` as a cursor and only enqueues videos newer than that.

## Workflows

### Add one creator

```bash
tiktok-archive creator add someuser --sync-depth last-6mo
tiktok-archive creator sync someuser     # initial pull
tiktok-archive worker --once             # drain the queue
```

### Bulk-import from your TikTok export

```bash
tiktok-archive creator import-from-export ~/Downloads/tiktok-export.zip --min-videos 3
tiktok-archive creator list              # confirm
tiktok-archive creator sync --all        # initial pull for all
tiktok-archive worker                    # run continuously to drain
```

`--min-videos N` filters to creators you've watched at least N times in your export. This avoids importing one-off views.

### Maintain creators.yaml

```bash
tiktok-archive creator export-to-yaml    # dump DB → creators.yaml
# edit creators.yaml in your editor
tiktok-archive creator import-from-yaml  # apply changes back to DB
```

`creators.yaml` is the version-controllable source of truth.

## Sync depth

| Depth      | Initial pull           | Subsequent pulls     |
| ---------- | ---------------------- | -------------------- |
| `full`     | All videos available   | Up to 50 newer       |
| `last-6mo` | Up to 200 most recent  | Up to 50 newer       |
| `last-50`  | 50 most recent         | Up to 50 newer       |

The default is `last-6mo`, which is what you want for most followed accounts. `full` should be used sparingly — pulling 5,000 videos from one creator will hammer TikTok's rate limit.

## Sync cadence

- Default: each creator is synced no more than once every 24 hours (`TT_CREATOR_SYNC_INTERVAL_HOURS`).
- `tiktok-archive creator sync --all` skips creators synced within that window.
- `--force` overrides the interval check.

## Triggering syncs

| How                                          | When                              |
| -------------------------------------------- | --------------------------------- |
| `tiktok-archive creator sync someuser`       | Manual: one creator now           |
| `tiktok-archive creator sync --all`          | Manual: all due creators          |
| Web UI → /creators → sync button             | Manual: enqueues a `sync-creator` job |
| `scripts/sync-and-drain.sh` via cron/launchd | Scheduled                         |

## Failure handling

- A creator sync that fails (rate limit, profile deleted, etc.) is recorded in `creators.sync_error` and `creators.sync_error_count`.
- The web UI shows an `error` tag on creators with non-empty `sync_error`.
- Errors are cleared on the next successful sync.
- `tiktok-archive creator disable someuser` stops syncing without deleting history.

## Rate limit safety

All TikTok requests (single-video downloads + creator profile crawls) share a global throttle. If we get a 429 or 403, every TikTok request pauses for `TT_RATE_LIMIT_PAUSE_SEC` (default 1 hour). Don't try to outsmart this — TikTok will IP-ban you.
