# Troubleshooting

## Setup

### `tiktok-archive check` says `whisper: MISSING — No Whisper backend available`

Install the right extras for your platform:
- Apple Silicon: `pip install -e ".[analyze-mac]"`
- NVIDIA / WSL2: `pip install -e ".[analyze-cuda]"`

If you installed `[analyze-mac]` but it's still failing, verify `uname -m` returns `arm64`. On Intel Macs, `mlx-whisper` won't install — use `[analyze-cuda]` for the faster-whisper CPU fallback.

### `ffmpeg not found on PATH`

```bash
brew install ffmpeg                       # macOS
sudo apt install -y ffmpeg                # Debian/Ubuntu/WSL2
```

Verify: `which ffmpeg` should print a path.

### `Ollama not reachable at http://localhost:11434`

```bash
brew services start ollama                # macOS
sudo systemctl start ollama               # Linux
```

Check it's responding: `curl http://localhost:11434/api/tags`.

### `model 'qwen2.5:7b' not found`

```bash
ollama pull qwen2.5:7b
```

### `tiktok-archive: command not found`

You forgot to activate the venv:

```bash
source venv/bin/activate
which tiktok-archive    # should print {project}/venv/bin/tiktok-archive
```

## Analyzing

### `yt-dlp succeeded but no video file found on disk`

This was the Phase 1.5 root-cause bug. `--dump-single-json` implies `--simulate` unless you also pass `--no-simulate`. The fix is baked into `downloader.py` since v0.2.0; if you're seeing this on a current install, file an issue.

### `IntegrityError: UNIQUE constraint failed: videos.url, videos.source, videos.collection_name`

A previous attempt for the same URL is still in the table. The fix is built in: `analyze_url` resets stuck `downloading`/`failed` rows to `pending`. If you somehow hit this manually:

```sql
sqlite3 data/tiktok.db "UPDATE videos SET download_status='pending', download_error=NULL WHERE download_status IN ('downloading','failed');"
```

### TikTok rate limited me

You'll see `rate_limited: true` in the result and a log line like:

```
WARNING Rate limited by TikTok (HTTP Error 429: Too Many Requests). Pausing all downloads for 3600s.
```

The global throttle now blocks **all** downloads for the configured pause (default 1 hour). Wait it out. If you absolutely need to retry sooner, restart the worker (state lives in process memory) — but you'll likely just get rate-limited again. Don't fight TikTok; the throttle exists because you'll get IP-banned otherwise.

### Whisper is taking forever

First run downloads the model (~1.5 GB for `medium`). Subsequent runs are 5-10× faster. Check `~/.cache/huggingface/` to see the model files.

If you want faster transcription:
- Switch to `TT_WHISPER_MODEL=small` (less accurate but ~2× faster).
- On NVIDIA: confirm `tiktok-archive check` reports `torch_device: cuda`. If it says `cpu`, your torch install isn't using the GPU.

### Q&A returns "The transcript does not mention this." for things that ARE in the transcript

Two common causes:
1. The transcript got truncated to 8000 chars. For long videos, the answer might be past the cutoff. Use the CLI: `tiktok-archive ask <id> "..."` doesn't have a Web UI middleman, but it still uses the same truncation. Lower `_QA_PROMPT_TEMPLATE` truncation by editing `process/qa.py`.
2. The model is being overly conservative. Check the actual transcript on the detail page — sometimes the LLM thinks something isn't there when it is.

## Worker

### Worker exits immediately on startup

It's printing `Reset N stuck jobs` then "queue is empty" and exiting? You probably ran `--once`. Without `--once`, it should run forever. Check `tiktok-archive stats` to see if there's actually anything pending.

### A job has been "running" for hours and isn't progressing

The worker probably crashed mid-job. Wait `JOB_LOCK_TIMEOUT_SEC` (30 min default) and start a new worker — it'll auto-reset stuck jobs. Or force it now:

```sql
sqlite3 data/tiktok.db "UPDATE jobs SET status='pending', locked_at=NULL, locked_by=NULL WHERE status='running';"
```

### `database is locked` errors

WAL mode + busy_timeout=5000 should prevent this for our workload. If you're hitting it:
1. Don't run multiple worker processes against the same DB.
2. Check for stale processes: `ps aux | grep tiktok-archive`.
3. If the DB is on a network drive (NFS/SMB), move it to local disk. SQLite + network filesystems = pain.

## Web UI

### Port 5050 already in use

Another instance is running, or another app grabbed it.

```bash
lsof -ti:5050 | xargs kill -9
# or change the port:
TT_WEB_PORT=5051 tiktok-archive serve
```

### Page loads but shows no recent videos

Either you genuinely have no videos yet, or the SQL query is failing (check the worker terminal for stack traces). Confirm with `tiktok-archive stats` — if the CLI shows videos and the web UI doesn't, file an issue.

## R2

### `setup-r2` says credentials are missing

`.env` isn't being loaded. Check:
1. `.env` is in the project root (same dir as `pyproject.toml`).
2. No quotes around values: `R2_ACCESS_KEY_ID=abc123` not `R2_ACCESS_KEY_ID="abc123"`.
3. `TT_STORAGE_BACKEND=r2` is set.

### `setup-r2` says `An error occurred (InvalidAccessKeyId)`

Either:
1. Wrong access key (typo).
2. Token was revoked.
3. Token doesn't have access to this bucket. R2 tokens are bucket-scoped; you may have created a token for a different bucket. Re-create one.

### `An error occurred (NoSuchBucket)`

`R2_BUCKET_NAME` doesn't match the actual bucket name in your Cloudflare dashboard. Names are case-sensitive.

### Mirror writes are slow

That's R2 round-trip latency — typically 100-300ms per put. Consider it free durability insurance. If it's blocking your workflow, set `TT_STORAGE_BACKEND=local` and run a periodic batch sync instead (Phase 2 enhancement).

## SQLite + macOS specific

### `OperationalError: attempt to write a readonly database`

The `data/` directory or `tiktok.db` file is owned by another user (or root, if you ran something with `sudo`). Fix permissions:

```bash
sudo chown -R $(whoami) data/
```

### `OperationalError: no such column: videos.transcript_only`

You upgraded from Phase 1.5 → 1.6 but haven't run anything that hits `init_db()`. Run `tiktok-archive check` — it calls `init_db()` which creates new columns via `Base.metadata.create_all` (additive only; it never drops columns).

For destructive schema changes in the future, we'd need a real migration tool. For now, `create_all` is sufficient.

## Photo posts and silent videos

### A photo post got summarized as "no transcribable audio"

That's expected behavior when the post is music-only or silent (slideshow with text overlay but no voiceover). The pipeline detects transcripts shorter than 20 characters and writes a stub summary instead of feeding garbage to Ollama. The row still exists with its URL, handle, and post id — just without semantic content for tag/topic/Q&A.

If you want to recover semantic content from a text-overlay photo post, you'd need OCR on the slide images. That's a separate feature, deferred for a future phase.

### A normal video got the same stub summary

Same root cause: Whisper produced a transcript shorter than 20 characters. The video probably has only background music with no speech. The stub is honest — there's nothing to summarize.

If you think there *should* be speech in the video (e.g., a quiet voiceover Whisper missed), try:
1. Re-analyzing with `--keep-video` and inspecting the saved `.mp4`.
2. Manually running `ffmpeg` to extract audio and listen to it:
   ```
   ffmpeg -i scratch/<id>.mp4 -ac 1 -ar 16000 audio.wav
   ```
3. If audio is clearly there but Whisper got nothing: switch to a larger model (`TT_WHISPER_MODEL=large` in `.env`).

## Photo posts fail with "Plain fetch returned anti-bot page and Playwright is unavailable"

You need to install Playwright and the Chromium binary:

```
source venv/bin/activate
pip install -e ".[browser]"
playwright install chromium
```

Verify with `tiktok-archive check` — you should see `playwright: OK` near the bottom.

## Photo posts fail with "Rehydration script never appeared (likely captcha)"

The browser successfully launched but TikTok served a captcha challenge page. This is rare on residential IPs but possible. Wait an hour and retry. If it persists, your IP may need to clear via TikTok's normal browsing — open the URL in regular Chrome, view a few posts, then retry the analyzer.

## "Pillow not installed" warnings during analyze

Install the media extras:
```
pip install -e ".[media]"
```
Without Pillow, all thumbnail extraction is skipped and the analyze pipeline still completes (just without images).

## "PySceneDetect not installed; skipping scene-change frames"

Same fix as above. Without scenedetect, important videos still get uniform thumbs but no scene-change full frames.

## My disk is filling up with full-res frames

Drop the LLM importance judge for one post, or unmark the noisy creator:
1. Open the video's detail page → Unmark Important. Full-res artifacts get deleted, thumbs stay.
2. Or remove `important: true` from the creator's entry in creators.yaml and run `tiktok-archive creator import-from-yaml`.

The auto-rule "empty transcript → important" still fires regardless. If you have many silent videos that you don't want full-res for, mark them unimportant individually.

## Photo posts fail with "Rehydration script never appeared (likely captcha or expired session)"

Run the auth flow once:

```
source venv/bin/activate
tiktok-archive auth-tiktok
```

A Chromium window opens at the TikTok login page. Log in normally (username/password, SMS code, etc). When you can see your TikTok feed, return to the terminal and press Enter. Future photo fetches will use that saved session.

If photo fetches start failing again later, your session expired — run `tiktok-archive auth-tiktok` again. Sessions usually last weeks but can be revoked at any time.

## My TikTok account got logged out / I get a 2FA prompt every fetch

Don't: keep a 2FA-required account permanently logged in via this tool — TikTok may detect the long-lived session as suspicious. Better: use a secondary account if possible, or accept that you'll re-auth every couple of weeks.

## I want to use this on a different Mac / wipe the saved session

Delete the profile dir:

```
rm -rf data/playwright-profile/
```

Then re-run `tiktok-archive auth-tiktok` to re-authenticate. The dir is just cookies and localStorage — nothing else is stored there.

## Photo posts fail even with `tiktok-archive auth-tiktok` saved session

This is a known limitation as of v1.7.3. Even when the Playwright fetcher uses a logged-in session (verified by the `auth=True` log line and the fetched page being ~3 KB larger than the anonymous version), TikTok still serves the anti-bot shell page for `/photo/` URLs in headless mode. The post item never appears in the rehydration JSON.

The current architecture treats this as an unrecoverable per-URL failure and moves on. Photo posts will be reported as `FAIL at download — Could not locate item data in rehydration JSON` and skipped.

Future paths to fix this (none implemented in v1.7.x):
- Run Playwright in non-headless mode permanently for /photo/ URLs.
- Use a residential proxy with a non-cloud IP.
- Switch to a different photo-fetch strategy (mobile API endpoints, intercepting in-page XHRs, etc.) — likely lands as part of the multi-source rewrite.

For now: photo URLs in your archive will end up in a `failed` state with this specific error message. They're easy to filter out with `select * from videos where post_type='photo' and download_status='failed'` if you want to see them, or just ignore them.
