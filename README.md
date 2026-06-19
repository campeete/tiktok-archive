# media-archive

Local-first multi-source media archive. Pulls posts/videos from TikTok and YouTube, transcribes them with Whisper (chunked for long-form), summarizes and tags them with a local LLM, stores everything in SQLite, and **groups curated subsets into collections that can be exported as a single blob for use with Claude or any other LLM.** No video data leaves your machine; transcripts can optionally mirror to Cloudflare R2.

**v0.3.0 — Collections + markdown export.** Adds a curation layer on top of the archive. Bundle related posts (by creator, by topic, by hand) into named collections. Export the whole collection — summaries, key points, topics, optionally full transcripts — to markdown, JSON, or plain text in one command. Paste the result into a Claude conversation and have it reason across the entire group at once.

## What v0.3.0 adds

| | v0.2.x | v0.3.0 |
| - | - | - |
| Curation | None | Named `Collection` table with ordered membership |
| Bulk add | None | `add-by-creator`, `add-by-topic` (date-range coming) |
| Export | None | Markdown / JSON / text, compact-default with `--full` flag |
| Use with Claude | Manual copy-paste of individual posts | Single export blob with self-describing footer |

## Roadmap

- **v0.1.0:** Rename + reorganize. *Done.*
- **v0.2.0:** YouTube + chunked Whisper. *Done.*
- **v0.2.1:** YouTube tag-write hotfix. *Done.*
- **v0.3.0 (this release):** Collections + export. *Done.*
- **v0.3.x:** Date-range bulk add, webapp Collections UI, "smart" auto-refreshing collections.
- **v0.4.0:** HTTP/JSON API.
- **v0.5.0:** MCP server. The first MCP tool will be `get_collection(name)` — same shape as the export module produces here.
- **v0.6.0:** Agent audit log + scoped API keys.

## Quickstart for v0.2.1 users

```
cd ~/Documents
unzip ~/Downloads/media-archive-v0.3.0.zip
cd media-archive-v0.3.0
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[analyze-mac]"
pip install -e ".[r2]"
pip install -e ".[browser]"
pip install -e ".[media]"
mkdir -p data
cp ~/Documents/media-archive-v0.2.1/data/tiktok.db data/tiktok.db 2>/dev/null
cp -r ~/Documents/media-archive-v0.2.1/data/playwright-profile data/playwright-profile 2>/dev/null
media-archive check
```

## Try collections

```
media-archive collection create offsec-notes --description "Cybersecurity research"
media-archive collection add offsec-notes "https://www.tiktok.com/@offsec/video/200"
media-archive collection add-by-creator offsec-notes redtales90
media-archive collection add-by-topic offsec-notes security
media-archive collection show offsec-notes
media-archive collection export offsec-notes --out ~/Desktop/offsec-archive.md
```

The exported markdown file is the artifact you paste into a Claude conversation. Each post in the collection appears as a numbered section with summary, key points, topics, and source URL. Add `--full` to include transcripts. Add `--format json` for structured output.

## v1.x history (preserved below for reference)

```
media-archive analyze "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

For long-form content (podcasts, lectures), the analyzer auto-detects duration and chunks the audio:

```
media-archive analyze "https://www.youtube.com/watch?v=PODCAST_VIDEO_ID"
```

You'll see log lines like `Long video (4823.1s >= 1500s); chunked transcription` and `Transcribing chunk 1/4 (offset=0.0s)` for each chunk as it processes. Memory stays bounded; quality stays high.

## Mixed-source bulk analyze

Both TikTok and YouTube URLs can be in the same URL list — the dispatcher handles routing per line:

```
echo "https://www.tiktok.com/@user/video/123
https://www.youtube.com/watch?v=abc
https://youtu.be/xyz" > urls.txt

media-archive analyze-bulk urls.txt --inline
```

Output:
```
Found 3 URLs (1 tiktok, 2 youtube) in /path/to/urls.txt.
[1/3] [tiktok] https://www.tiktok.com/@user/video/123
  OK in 42.5s — ...
[2/3] [youtube] https://www.youtube.com/watch?v=abc
  OK in 38.1s — ...
[3/3] [youtube] https://www.youtube.com/watch?v=xyz
  OK in 51.0s [3 chunks] — ...
Done. 3 succeeded, 0 failed.
```

## v1.x history (preserved below for reference)

## v1.x history (preserved below for reference)

The original v1.x README content follows. All of it still describes accurate behavior — the rename is purely a refactor.

---


Local-first TikTok analyzer. Pulls a video, transcribes it with Whisper, summarizes and tags it with a local LLM, and stores everything in SQLite. No video data leaves your machine; the resulting transcripts can optionally mirror to Cloudflare R2 for off-machine durability.

**Phase 1.6.3** adds proper TikTok photo-post support: fetches the post page directly, transcribes audio (voiceover) when present, OCRs slide images (when tesseract is installed) when audio is silent. yt-dlp doesn't handle photo posts, so we go around it.

**Phase 1.6.2** adds basic photo-post detection and a bulk-analyze command for processing lists of URLs.

**Phase 1.6.1** keeps everything from 1.6 (creator sync, job queue, transcribe-and-discard, R2) and rebuilds the web UI under the standard project frontend template (black/white, borders only, no external libs).

```
$ tiktok-archive analyze https://www.tiktok.com/@user/video/...
... transcript + summary + tags in ~30-90s on Apple Silicon
$ tiktok-archive serve
... web UI on http://127.0.0.1:5050
```

## Features

- **Single-video analyze**: paste a URL, get back transcript + summary + tagged topics + Q&A.
- **Creator sync**: register creators, periodically pull their new videos, drain through a worker.
- **Transcribe-and-discard**: videos are deleted after transcription. Disk usage scales with text, not video.
- **Local LLMs only**: Whisper for ASR, qwen2.5:7b via Ollama for summary/tagging/Q&A.
- **Apple Silicon native**: mlx-whisper backend on M-series Macs. CUDA backend (faster-whisper) on NVIDIA.
- **Web UI**: terminal-aesthetic Flask app on `127.0.0.1:5050` for browsing and Q&A.
- **R2 mirror**: optional Cloudflare R2 backend mirrors transcripts and DB backups for durability. Local always primary.

## Quickstart (Apple Silicon)

```bash
# 1. clone
git clone git@github.com:campeete/tiktok-archive.git
cd tiktok-archive

# 2. python 3.12 + venv
brew install python@3.12 ffmpeg tesseract     # tesseract is optional, enables photo OCR
python3.12 -m venv venv
source venv/bin/activate

# 3. install
pip install -e ".[analyze-mac]"

# 4. ollama
brew install ollama
brew services start ollama
ollama pull qwen2.5:7b

# 5. config
cp .env.example .env       # edit if needed; defaults work out of the box
cp tags_vocabulary.example.yaml tags_vocabulary.yaml  # optional: customize topics

# 6. verify
tiktok-archive check
```

You should see `whisper: OK mlx`, `ollama: OK`, `yt-dlp: <version>`. If anything is MISSING, follow the message — it'll tell you the exact fix.

## Quickstart (NVIDIA / WSL2)

```bash
# After installing CUDA + ffmpeg + Ollama:
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[analyze-cuda]"
ollama pull qwen2.5:7b
tiktok-archive check
```

## Usage

### Analyze a single video

```bash
tiktok-archive analyze https://www.tiktok.com/@user/video/123...
# Photo posts are also supported:
tiktok-archive analyze https://www.tiktok.com/@user/photo/456...
```

### Analyze a batch of URLs from a file

```bash
# Save URLs (one per line, '#' for comments) to urls.txt, then:
tiktok-archive analyze-bulk urls.txt              # enqueue + drain with worker
tiktok-archive analyze-bulk urls.txt --inline     # process sequentially in foreground
```

### Browse the web UI

```bash
tiktok-archive serve
# → open http://127.0.0.1:5050
```

### Register creators and sync

```bash
tiktok-archive creator add someuser --sync-depth last-6mo
tiktok-archive creator list
tiktok-archive creator sync someuser
tiktok-archive creator sync --all
```

### Run the worker (drains queued downloads/transcribes)

```bash
# Forever:
tiktok-archive worker

# Once and exit (cron-friendly):
tiktok-archive worker --once
```

### Bulk-import a TikTok data export

```bash
# 1) ingest the videos themselves
tiktok-archive ingest ~/Downloads/tiktok-export.zip

# 2) seed creators.yaml from the same export
tiktok-archive creator import-from-export ~/Downloads/tiktok-export.zip --min-videos 3

# 3) drain
tiktok-archive worker
```

### Pipeline state

```bash
tiktok-archive stats
```

## Architecture

```
┌───────────────────┐     ┌─────────────────────┐
│  CLI / Web UI     │────▶│  SQLite (videos,    │
│  analyze, serve   │     │  creators, jobs)    │
└─────────┬─────────┘     └──────────┬──────────┘
          │                          │
          ▼                          ▼
┌───────────────────┐     ┌─────────────────────┐
│  Worker process   │────▶│  scratch/ (mp4)     │
│  drains job queue │     │  ↓ delete on success│
└─────────┬─────────┘     └──────────┬──────────┘
          │                          │
          ▼                          ▼
┌───────────────────┐     ┌─────────────────────┐
│  Whisper          │     │  transcripts/*.json │
│  Ollama (tag/Q&A) │────▶│  durable, mirrorable│
└───────────────────┘     └──────────┬──────────┘
                                     │
                                     ▼ (optional)
                          ┌─────────────────────┐
                          │  Cloudflare R2      │
                          └─────────────────────┘
```

See [`docs/architecture.md`](docs/architecture.md) for details.

## Storage philosophy

The video file (`.mp4`) lives in scratch only as long as Whisper needs it. After the transcript is written, the video is deleted. **The transcript is the asset.** This means 50,000 videos uses ~500 MB of disk for transcripts (about 10 KB each), not 500 GB for videos.

If you need to re-derive anything later, the URL is stored — you can re-download.

## Cloudflare R2 (optional)

R2 is a pure backup. Transcripts and metadata are written locally first, then mirrored to R2. Reads always hit local first. If R2 disappears, you lose nothing.

Setup:

1. Create a bucket on Cloudflare R2.
2. Create an API token scoped to that bucket (Object Read & Write).
3. Add to `.env`:
   ```
   TT_STORAGE_BACKEND=r2
   R2_ACCOUNT_ID=...
   R2_ACCESS_KEY_ID=...
   R2_SECRET_ACCESS_KEY=...
   R2_BUCKET_NAME=tiktok-archive
   ```
4. `pip install -e ".[r2]"`
5. `tiktok-archive setup-r2`  → tests the connection

See [`docs/r2-setup.md`](docs/r2-setup.md) for the full walkthrough.

## Roadmap

- **Phase 1.5** ✅ Single-video analyze + web UI
- **Phase 1.6** ✅ Creator sync + job queue + R2 backend
- **Phase 1.6.1** ✅ Web UI rebuilt under the standard frontend template
- **Phase 1.6.2** ✅ Bulk-analyze command
- **Phase 1.6.3** ✅ Native photo-post support (audio + OCR)
- **Phase 1.7** ⏳ Bulk-process a full TikTok data export
- **Phase 2** ⏳ Background download/transcribe scheduling
- **Phase 3** ⏳ Auto-tagging refinement, custom vocabularies per archive
- **Phase 5** ⏳ ChromaDB embeddings + semantic search across the archive
- **Phase 6+** ⏳ MCP agent integration

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — system overview
- [`docs/api.md`](docs/api.md) — HTTP API reference
- [`docs/analyze.md`](docs/analyze.md) — single-video pipeline
- [`docs/creator-sync.md`](docs/creator-sync.md) — creator sync details
- [`docs/queue.md`](docs/queue.md) — job queue + worker
- [`docs/r2-setup.md`](docs/r2-setup.md) — R2 configuration
- [`docs/deploy.md`](docs/deploy.md) — install on Mac, WSL2, Linux
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — known issues + fixes
- [`docs/decision-log.md`](docs/decision-log.md) — design decisions
- [`docs/capacity-plan.md`](docs/capacity-plan.md) — disk/throughput planning

## License

MIT
