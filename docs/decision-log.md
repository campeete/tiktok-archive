# Decision log

A running record of architectural decisions, the alternatives considered, and the reasoning. New decisions append at the bottom; old decisions stay for context.

---

## D-001: Local-first, no cloud LLMs

**Decision:** All LLM calls go to a local Ollama instance. No OpenAI/Anthropic API calls in the analyze pipeline.

**Alternatives:** GPT-4 / Claude APIs would give better summaries.

**Why:** Cost (free vs. ~$0.001/video × thousands), privacy (transcripts can include personal context), and the project is part of a federal-clearance-track portfolio where "no third-party data sharing" is meaningful. Some quality is worth trading.

---

## D-002: SQLite over Postgres

**Decision:** SQLite with WAL mode is the storage layer.

**Alternatives:** Postgres for "real" concurrency.

**Why:** This is a single-user, local-machine app. SQLite is one file, no daemon, and WAL mode handles a worker + web UI concurrently fine. If we ever cross "single user" we revisit, but I'd rather wait until then.

---

## D-003: yt-dlp over scraping libraries

**Decision:** All TikTok HTTP goes through `yt-dlp`.

**Alternatives:** `TikTokApi`, custom scraper, third-party services.

**Why:** yt-dlp is the most actively maintained downloader and tracks TikTok's anti-scraping changes the fastest. We pay a process-spawn cost per call, which is fine.

---

## D-004: Controlled vocabulary for topics

**Decision:** Topics come from a YAML file the user edits. The LLM is told which topics are allowed and cannot invent new ones.

**Alternatives:** Free-form tags, hierarchical taxonomy from someone else's research.

**Why:** Free-form tags lead to "AI", "ai", "artificial intelligence" all being separate. An external taxonomy doesn't fit personal archives. YAML is editable, version-controllable, and the constraint dramatically improves consistency over thousands of videos.

---

## D-005: Two Whisper backends

**Decision:** `mlx-whisper` on Apple Silicon, `faster-whisper` everywhere else, selected at runtime.

**Alternatives:** Pick one and require everyone to install the same backend.

**Why:** mlx-whisper is materially faster on M-series than torch-based options. faster-whisper on CUDA is materially faster than mlx on the same hardware (irrelevant since they're different machines, but a single backend would cripple one of them). Runtime selection costs nothing and keeps both fast.

---

## D-006: ffmpeg as a hard dependency

**Decision:** ffmpeg must be on PATH; we don't ship a Python alternative.

**Alternatives:** `pydub`, `librosa` for audio extraction.

**Why:** ffmpeg handles every codec TikTok throws at us; pydub wraps ffmpeg anyway; librosa is for analysis, not extraction. A 10MB system dep is acceptable.

---

## D-007: One Ollama model serves tagging, summary, Q&A

**Decision:** `qwen2.5:7b` is the default for all three tasks (`TT_TAG_MODEL`, `TT_SUMMARY_MODEL`, `TT_QA_MODEL`).

**Alternatives:** Specialized models per task.

**Why:** Loading multiple 7B models trashes RAM. qwen2.5:7b handles all three tasks well enough. Configurable via env, so users can specialize if they want.

---

## D-010: Shared SQLite via Syncthing (optional)

**Decision:** For multi-machine setups, Syncthing the `data/` directory rather than building a sync protocol.

**Alternatives:** Build cross-machine sync into the app.

**Why:** Syncthing exists, is reliable, and handles SQLite-with-WAL correctly. We'd be reinventing a worse version of it.

---

## D-011: 4070 PC for inference, Mac for query

**Decision:** When two machines are available, the heavier one runs the worker (drains the queue) and the lighter one runs the web UI (browses results).

**Alternatives:** Symmetric — either machine does both.

**Why:** Whisper transcription benefits massively from a 4070 vs M-series base GPU. The web UI is read-heavy and lightweight. Splitting them lets us match each workload to its optimal hardware without complicating the code.

---

## D-012: Phase 1.5 ships before Phase 2

**Decision:** Single-video analyze + web UI shipped before the bulk-export pipeline.

**Alternatives:** Build the full export pipeline first.

**Why:** I needed to validate the whole vertical (download → transcribe → tag → display) on real videos before scaling to thousands. Found 7 bugs that would've been catastrophic at scale.

---

## D-013: Flask + Jinja + vanilla JS for the web UI

**Decision:** No React, no SPA, no build step.

**Alternatives:** React + Vite, HTMX.

**Why:** The UI is 4 pages and a few interactions. A bundler would dwarf the actual code. Vanilla JS plus server-rendered Jinja keeps the project install-pip-and-run.

---

## D-014: Web UI binds to 127.0.0.1 only

**Decision:** The web UI is not network-accessible by default.

**Alternatives:** Bind to `0.0.0.0` and add auth.

**Why:** Auth is a real feature with real attack surface. 127.0.0.1 default + a single env var to override is the right tradeoff for a local-first tool. If users want to expose it, they should put a real auth proxy (Tailscale, Caddy + basic auth) in front.

---

## D-015 (Phase 1.6): Job queue in SQLite, single-process worker

**Decision:** The work queue is a `jobs` table in the same SQLite DB. One worker process drains it.

**Alternatives:** Redis + RQ, Celery, custom thread-based queue without persistence.

**Why:**
- Redis is a daemon to install + manage. We don't have a Redis-shaped problem yet.
- Celery is a sledgehammer for a peanut.
- Threads-only loses everything on crash; we want persistence.
- SQLite + atomic UPDATE-with-WHERE pattern is a battle-tested queue pattern (Litestream, sqlc, Litequeue all do this). The DB lock is the queue lock. Free.

If we ever need multi-machine workers, we already have R2 and could move the queue out then. For now: simple, durable, fast.

---

## D-016 (Phase 1.6): Transcribe-and-discard

**Decision:** After Whisper finishes, the source `.mp4` is deleted. The transcript JSON is the canonical artifact.

**Alternatives:** Keep videos forever; keep videos for a configurable TTL.

**Why:**
- Disk math: 50K videos × 10MB ≈ 500GB of videos. 50K × 10KB transcripts ≈ 500MB. 1000× difference.
- The video is recoverable: we keep the URL. Worst case, re-download.
- Transcripts are sufficient for everything we want to do (search, Q&A, embeddings).
- This is the single biggest scaling decision. Without it, we'd be building a video archive — a different project.

`--keep-video` is available for analyses where the user explicitly wants the file (debugging a transcribe failure, training data extraction, etc.).

---

## D-017 (Phase 1.6): R2 mirrors transcripts, never videos

**Decision:** When `STORAGE_BACKEND=r2`, transcripts and DB backups go to R2. Videos do not.

**Alternatives:** Mirror everything to R2; mirror nothing.

**Why:**
- R2 free tier is 10GB. 50K transcripts ≈ 500MB; fits with 95% headroom. 50K videos at 10MB = 500GB; doesn't fit, would cost real money.
- Uploading a 10MB video then re-downloading it on the laptop wastes bandwidth and defeats local-first.
- Videos are ephemeral by design (D-016). Mirroring something we're about to delete is incoherent.

---

## D-018 (Phase 1.6): Bounded concurrency per stage

**Decision:** Per-stage limits in config: download=1, transcribe=1, tag=2, embed=2.

**Alternatives:** Unbounded thread pool; one global limit.

**Why:**
- TikTok rate-limits aggressively; >1 concurrent download = guaranteed 429.
- Whisper saturates the GPU; >1 concurrent transcribe = thrashing, often slower than serial.
- Ollama can serve 2 generations concurrently on most setups; tag and embed benefit.
- Per-stage limits let us tune each independently without breaking the others.

---

## D-019 (Phase 1.6): Idempotent operations everywhere

**Decision:** Every operation (add creator, enqueue job, analyze URL) is idempotent — re-running it doesn't break anything or duplicate work.

**Alternatives:** "Caller is responsible for not retrying."

**Why:**
- Workers crash; users hit "sync" twice; cron runs while a manual run is happening. Idempotency at every level means none of these scenarios produce bad data.
- The cost is small: a `WHERE` clause in most cases, an `INSERT ... ON CONFLICT` in others.
- It's much cheaper to design idempotency in than to retrofit it.

---

## D-020 (Phase 1.6): First-sync depth defaults to last-6mo

**Decision:** When adding a new creator, the default sync depth pulls roughly the last 6 months of their content.

**Alternatives:** `full` (everything), `last-50` (a fixed count).

**Why:**
- `full` on a prolific creator (10+ posts/day for years) is a 5K-video pull. Hammers TikTok, takes days, dilutes the archive with stale content.
- `last-50` is too short for occasional posters.
- `last-6mo` (with a 200-video cap as a safety net) catches active creators recently and lets one-a-week posters' archives go back further. Good default.

User can override per-creator. Most don't need to.

---

## D-021 (Phase 1.6.1): Frontend follows the standard template

**Decision:** All frontend pages — current and future — follow the standard project template: black background, white text, borders only for structure, only black/white (no gradients/colors), generous padding/margins, CSS-only animated horizontal progress bars, no external libraries. Layout is `header / nav (20%) / main (60%) / aside (20%) flex / footer`.

**Alternatives:** Continue the amber-on-black terminal aesthetic from Phase 1.5; let each project pick its own visual language.

**Why:** Consistency across all of my projects matters more than per-project aesthetic flair. The template is intentionally restrictive: it removes design decisions from every new build, forces information density to come from typography and spacing rather than color, and ensures any UI I ship looks coherent next to the others. The amber aesthetic was nice but it was a one-off — the template wins on long-term cost.

The Phase 1.5 amber UI was rebuilt under this template as part of the 1.6.1 patch. Future frontends start from the template by default.

---

## D-022 (Phase 1.6.2): Photo posts route through the same pipeline as videos

**Decision:** TikTok photo carousel posts (`/photo/<id>` URLs) go through the same `analyze_url` pipeline as videos. yt-dlp downloads them; if the post has a voiceover, Whisper transcribes it normally. If the result is a transcript shorter than 20 characters, we skip the Ollama tag pass and write a stub summary noting "no transcribable audio (music-only or silent slideshow)."

**Alternatives:** Build a separate photo-post pipeline with OCR on slide text. Skip photo posts entirely.

**Why:**
- Most informative photo posts have voiceovers — those are equivalent to videos as far as our pipeline cares.
- Music-only / silent photo posts have nothing to transcribe; running them through Ollama would just hallucinate a summary from no input. The stub summary is honest.
- OCR on slide text is a real feature with real cost (extra dependency, extra failure mode, extra disk for slide image extraction). Defer to a later phase if/when it becomes a real need.
- Keeping one pipeline is cheaper than maintaining two.

The empty-transcript stub also catches normal videos that have no spoken audio (music-only edits, ambient clips). Same handling, same row state — `tagged_at` is set, `_stub: true` recorded in `tag_summary` JSON for analytics.

---

## D-023 (Phase 1.6.2): `analyze-bulk` separates enqueue from execution

**Decision:** The new `analyze-bulk <file>` command parses a URL list and enqueues `download` jobs by default. The worker drains them. With `--inline`, URLs are processed sequentially in the foreground.

**Alternatives:** Always run inline. Always enqueue.

**Why:**
- Enqueue is the right default for >5 URLs: the worker handles rate limits, backoff, and crash recovery. Run the worker overnight; check `tiktok-archive stats` in the morning.
- Inline is the right default for small batches and testing: you see results stream in, errors are immediately obvious, and you don't need a separate worker terminal.
- Splitting the modes costs ~30 lines and is much more flexible than picking one.

The bulk command also dedupes URLs and skips ones already analyzed (rows where `tagged_at IS NOT NULL`), so re-running the same file is safe and idempotent. Failed-but-not-yet-tagged rows do get re-enqueued.

---

## D-024 (Phase 1.6.3): Native photo-post support via web HTML scraping

**Decision:** TikTok photo posts (`/photo/<id>` URLs) are handled by a custom processor that fetches the post HTML, parses the `__UNIVERSAL_DATA_FOR_REHYDRATION__` JSON blob, and extracts image URLs + audio URL directly from the CDN paths. yt-dlp does not support photo posts ("Unsupported URL"), so we go around it.

**Alternatives:**
1. Continue using the v1.6.2 detect-and-skip stub (do nothing). Rejected — Cameron's archive will have many photo posts; ignoring them loses real content.
2. Use `TikTokApi` (the davidteather Playwright wrapper). Rejected — it requires a running browser session, ms_token cookies, and adds Playwright as a heavy dependency. Overkill for our needs.
3. Use a third-party scraping API (Scrapfly, etc.). Rejected — paid, external, and a privacy concern (the URL list would leak to a third party).
4. Custom HTML scraping (chosen).

**Why:**
- The rehydration JSON is publicly served to any browser hitting the page; no auth required.
- We're running on a residential IP (Cameron's Mac), which is what TikTok's anti-bot is most lenient with.
- Browser-like headers + slow request rate (we're not crawling at scale) keeps us under the radar.
- The parser walks multiple known JSON paths and falls through gracefully to the empty-transcript stub if TikTok rotates keys, so a structural change degrades to "no content" rather than crashing the pipeline.

**Three transcript sources for photo posts:**
1. **Audio voiceover** — most informative. Whisper transcribes the audio track, same as for videos. This is the primary mechanism.
2. **OCR on slide images** — when audio is silent or music-only. Requires `tesseract` installed (`brew install tesseract`). Each slide downloaded to a tempdir, OCR'd, then discarded. Slide text is annotated `[Slide N]` so it's clear where each chunk came from.
3. **Combined audio + OCR** — when audio is too brief to be useful but slides have text. Both sources concatenated.

If neither source produces meaningful content, the pipeline writes the same stub summary as v1.6.2: "Photo post with no transcribable audio." Metadata (handle, image count, image URLs) is still preserved on the row.

**Risks accepted:**
- TikTok rotates the `__DEFAULT_SCOPE__` paths every few weeks. The parser checks 3 known paths; when all miss, the post fails cleanly. Maintenance load: occasional path-list update.
- TikTok ToS prohibits scraping. Personal-archival use of content the user already saw is the same standing as yt-dlp itself; we accept the same risk.
- The rehydration approach won't survive if TikTok ever moves to JS-only rendering (no SSR JSON). At that point we'd need Playwright. Today, the JSON is still in the page HTML.

**Why no slide image archival:**
Same reason we discard videos (D-016). Image carousels are 0.5-5MB each; a single 35-slide post could be 100MB+. Multiplied by even 50 photo posts in the archive, that's gigabytes for content we already extracted the meaning from. Image URLs are saved in the transcript JSON metadata, so the slides are recoverable on demand.

---

## D-025 (Phase 1.6.3 hotfix v0.2.4): Don't set Accept-Encoding manually

**Decision:** The photo-post HTTP fetcher no longer sends an explicit `Accept-Encoding` header. `requests` negotiates encoding itself and decompresses automatically. We also force `resp.encoding = "utf-8"` when the response doesn't specify a charset (TikTok often doesn't), to prevent ISO-8859-1 fallback corruption of the JSON blob.

**Why:** The first live photo-post fetch returned 66KB of mojibake — the response body was still gzip-compressed because we'd advertised `gzip, deflate, br` but `requests` won't transparently decode if the user supplies the header manually. Verified by debug-photo dump showing zero recognizable script tags and binary garbage in the response.

**Bonus changes shipped together:**
- The rehydration parser tries three known script-tag IDs in order: `__UNIVERSAL_DATA_FOR_REHYDRATION__`, `SIGI_STATE` (legacy), `__NEXT_DATA__` (Next.js).
- `_walk_to_item` handles the corresponding three top-level data shapes.
- New CLI command `tiktok-archive debug-photo <url>` dumps the diagnostic info I'd otherwise have to ask the user to gather via Python one-liner.

---

## D-026 (Phase 1.6.5): Playwright fallback for photo posts

**Decision:** Photo-post fetcher now tries plain `requests` first, then falls back to a real headless Chromium via Playwright when the rehydration JSON is missing the post item (the smoking gun for TikTok's anti-bot landing page).

**Why:** v0.2.4's debug-photo run on a real photo URL showed:
- `Has captcha-related: True` in the page HTML
- Top-level scope keys are only `webapp.app-context`, `webapp.biz-context`, `webapp.i18n-translation`, etc. — no item-detail anywhere
- This was not a key rotation; TikTok was serving the anti-bot shell

Header tweaks and key path additions can't fix this. Only running JS in a real browser gets past it.

**Architecture:** Plain fetch stays as the fast path (~0.5s). Browser fetch is invoked only on fallback (~5s). Videos still use yt-dlp — Cameron explicitly chose photo-only Playwright since the video pipeline is already working at 12.7s/video and adding Playwright there would be a regression.

**Trade-offs accepted:**
- ~150MB Chromium binary in the venv (one-time download)
- ~5s per photo-post fetch when fallback fires
- Playwright is an optional `[browser]` extra; videos work without it

**Install on Cameron's Mac:**
```
pip install -e ".[browser]"
playwright install chromium
```

**Open:** If TikTok ever does the same anti-bot to videos, we move yt-dlp behind a Playwright-driven URL extractor. For now, photo-only is enough.

---

## D-027 (Phase 1.7.0): Media artifact extraction with LLM importance judge

**Decision:** Every analyzed post produces image artifacts:
- Photo posts: thumbnail of every slide (256px JPEG), full-res slides only when important.
- Videos: uniform every-2s thumbnails, plus full-res scene-change frames only when important.

"Important" is decided by three rules in priority order:
1. Creator flagged `important: true` in creators.yaml → always important.
2. Empty/whitespace transcript → always important (slides ARE the message).
3. Otherwise, qwen2.5:7b is asked: "is the visual essential to the message?"

The user can override the judgment via the web UI at any time. Override is sticky — re-runs of the analyze pipeline will not blow it away.

**Why:** Cameron explicitly chose the LLM-judge path over my pushback (which favored cheap-rules-only). The LLM call adds 5-15s per post but saves operator effort vs. flagging by hand. Storage cost is bounded (free tier on R2 at current archive scale).

**Architecture:**
- New `MediaArtifact` table indexed by (video_id, kind, sequence). Idempotent upserts let us re-extract without DB churn.
- New `process/media.py` (~470 lines) handles ffmpeg uniform sampling, scenedetect scene-change frames, Pillow thumbnailing, R2 mirror, and the drop-full cleanup when a video is unmarked important.
- New `process/importance.py` (~135 lines) houses the judge.
- Pipeline order changed: transcribe → tag → judge → media-extract → discard. Discard moved from inline-after-transcribe to end-of-pipeline so media has access to the .mp4.
- Web UI: thumbnail grid on detail page, "Mark Important" / "Unmark" button, full-res served via /media/<artifact_id>.

**Trade-offs accepted:**
- Per-post LLM call (~5-15s) adds 1-3 hours to a 700-post overnight run.
- Pillow + scenedetect become required for v1.7 features (optional `[media]` extra).
- LLM defaults to false-on-failure → favors smaller storage; the empty-transcript rule still catches the most-important case independently.

**Alternatives considered:**
- Cheap-rules-only (recommended in pushback) — rejected by Cameron in favor of LLM judgment.
- Both-uniform-and-scene-change for everything — rejected on storage and redundancy grounds; scene-change runs only on important.
- Per-creator-only (no LLM) — rejected as too manual.

---

## D-028 (Phase 1.7.1): Persistent browser profile for photo-post auth

**Decision:** Photo-post fetches now run inside a Playwright persistent context anchored at `data/playwright-profile/`. The user runs `tiktok-archive auth-tiktok` once, manually logs into TikTok in a visible browser, and from then on subsequent fetches reuse that authenticated session in headless mode.

**Why:** v1.7.0's debug-photo run on Cameron's Mac proved that anonymous Playwright fetches return the same anti-bot shell that plain requests does. TikTok specifically gates `/photo/` URL post data behind a logged-in session. No header tweak, no fresh-context Chromium, no extra wait time clears that gate — only auth does.

**Architecture:**
- `BROWSER_PROFILE_DIR` config var, defaulting to `data/playwright-profile/`.
- `launch_persistent_context(user_data_dir=...)` replaces the previous `launch() + new_context()` pattern.
- `_have_saved_profile()` checks for `<profile>/Default/Cookies` (a non-empty SQLite file) as the canonical signal.
- New `tiktok-archive auth-tiktok` command launches headed Chromium → user logs in → user presses Enter in terminal → context closes, profile saves.
- `tiktok-archive check` reports session presence/absence under the playwright line.

**Trade-offs accepted:**
- Profile dir holds an authenticated TikTok session. Anyone with filesystem access to the Mac can use that session — same security posture as Cameron's regular Chrome cookies.
- Sessions can expire silently. The fetcher will produce a "session expired" error and direct the user to re-run `auth-tiktok`. We do not auto-detect expiry beyond the failed-fetch signal.
- Persistent context is slightly slower than fresh context (~6-8s vs ~5s) but the auth gain is worth it.

**What we didn't do:**
- Cookie-paste from Chrome DevTools — rejected as more fragile (manual export step every few weeks vs single one-time login).
- Storing auth in `.env` as a token — TikTok rotates session tokens too aggressively and they're tied to fingerprint/IP context that a token alone can't reproduce. The full cookie jar via persistent profile is more robust.

---

## D-029 (Phase 1.7.2): Reject non-TikTok URLs upfront, classify expected ffmpeg failures

**Decision:** The analyze command now rejects any URL whose host isn't TikTok before passing it to yt-dlp. The `extract_audio` step in transcribe.py now distinguishes between "no audio stream in input" (an expected failure mode) and other ffmpeg errors, raising `NoAudioStreamError` for the former so callers can suppress the stack trace.

**Why:** Cameron's stress-test of v1.7.1 surfaced two issues. (1) Pasting a YouTube URL into `tiktok-archive analyze` got past the URL normalizer (which silently dropped the `?v=...` query string, leaving `https://www.youtube.com/watch`), then yt-dlp fetched something, then ffmpeg crashed extracting audio because the file had no streams. (2) A TikTok URL with a stray trailing single-quote (zsh `dquote>` mode artifact) yt-dlp'd into a malformed download with the same downstream failure. Both surfaced as Python tracebacks even though the JSON output below was clean — `logger.exception()` includes the stack trace by default.

**Changes:**
- New `is_tiktok_url()` in `ingest/urls.py`. Accepts www., m., naked tiktok.com, and vm.tiktok.com short-link hosts.
- `normalize_tiktok_url()` now strips trailing punctuation (quotes, parens, periods, commas, etc.) before parsing.
- New `NoAudioStreamError` subclass of `RuntimeError` in `process/transcribe.py`. `extract_audio` raises it on known signatures: "Output file does not contain any stream", "Stream map ... matches no streams", "Invalid data found when processing input".
- `analyze_url` catches `NoAudioStreamError` and logs at `WARNING` level (no stack trace) — the user-visible JSON gets an extra `"error_type": "no_audio_stream"` field for callers to switch on.
- The CLI's `analyze` handler short-circuits with an `ok: false, stage: validate` result when given a non-TikTok URL.

**Trade-offs accepted:**
- The trailing-garbage stripper is greedy. If a TikTok URL ever legitimately ends in punctuation (it doesn't, the post ID is always digits), this would corrupt it.
- `is_tiktok_url` is permissive about subdomain (anything ending in `.tiktok.com` passes). This is intentional — TikTok has experimented with regional subdomains.

**What this unblocks:**
- Bulk-analyze runs against mixed-source URL files. Non-TikTok URLs now fail with a clean validate-stage error instead of crashing or producing weird half-baked DB rows.
- Future multi-source rewrite: same pattern (host classification → dispatch) will be the dispatcher's primary mechanism.

121/121 tests pass (was 106; +8 URL classification tests, +4 normalize-trailing-garbage tests, +3 transcribe error classification tests).

---

## D-030 (Phase 1.7.3): Bulk-analyze rejects non-TikTok URLs upfront, photo limitation documented

**Decision:** `analyze-bulk` now applies the same `is_tiktok_url` validation that single-URL `analyze` got in D-029. Non-TikTok URLs and unparseable lines are reported by category and skipped before the pipeline runs. The all-bad-input case returns a non-zero exit code instead of an empty success.

**Why:** Cameron's v1.7.2 stress test on bulk-analyze showed that a YouTube URL leaked through the bulk parser even though the equivalent single-URL command rejected it cleanly. Reading the code: bulk's parsing path called `normalize_tiktok_url()` directly without the `is_tiktok_url()` host check first, so the YouTube URL got mangled into `https://www.youtube.com/watch` (query stripped) and ran through the pipeline, downloading something useless before failing at the "no video file on disk" stage.

**Changes:**
- `cmd_analyze_bulk` calls `is_tiktok_url()` before normalizing each line.
- Rejected URLs and unparseable lines are reported separately with the first 3-5 examples each, so the user knows what got dropped.
- All-rejected file returns rc=1 and an explanatory stderr message.
- Output wording bumped from "Found X URLs" to "Found X TikTok URLs" since we now know they're all valid.

**Photo-post limitation documented:** Cameron's same v1.7.2 stress test confirmed that even an authenticated Playwright session (verified `auth=True`, page size 282K vs anonymous 279K) still gets the anti-bot shell from TikTok for `/photo/` URLs. The post item is never present in the rehydration JSON. This is a hard limit of the current architecture; it's now documented in troubleshooting.md as a known limitation rather than a bug to chase. Future fix lands as part of the multi-source rewrite where the photo plugin can use a different fetch strategy (non-headless permanent, mobile API, residential proxy).

125/125 tests pass (was 121; +4 bulk-parsing tests).

---

## D-031 (v0.1.0 / Phase 2 scaffolding): Rename `tiktok-archive` → `media-archive`, reorganize into core/sources/api

**Decision:** Rename the package, the CLI command, and the GitHub repo from `tiktok-archive` to `media-archive`. Reorganize the source tree from a flat single-source layout into a three-tier structure:
- `media_archive.core` — shared infrastructure (DB schema, queue, config, storage, webapp shell, query helpers, embedding index).
- `media_archive.sources.<name>` — per-source plugins. TikTok-specific code (ingest/, process/, sync/) is now under `sources.tiktok.*`.
- `media_archive.api` — HTTP/JSON server + MCP server (currently empty stubs; populated in later milestones).

The CLI dispatcher (`cli.py`) stays at the top level and routes commands to the right source.

Both `media-archive` and `tiktok-archive` are registered as console scripts pointing at the same `cli:main`, so v1.7.x users' shell muscle memory, launchd plists, cron jobs, and shell scripts continue to work without modification.

**Why now:** Phase 2 is going to add YouTube, then long-form chunked transcription, then HTTP+MCP API surfaces. Each of those changes is large enough that doing it on top of the v1.7.x flat single-source layout would mean either (a) intermingling YouTube code with TikTok code in the same modules, or (b) doing the rename mid-feature and dealing with merge pain. The locked Phase 2 plan estimated this scaffolding step at ~1 hour focused; doing it in isolation as v0.1.0 is much cheaper than doing it later.

**DB schema change:** Added `platform` column to `videos` (VARCHAR(30), NOT NULL, DEFAULT 'tiktok', indexed). Why a new column instead of repurposing `source`? Because `source` already exists in v1.7.x and means *ingestion path* ('analyzed' / 'creator-sync' / 'export' / 'bulk') — not content origin. Renaming the existing column would break v1.7.x DBs; reusing it semantically would be confusing. Adding `platform` with a default keeps both meanings clean. The migration is a single ALTER TABLE ADD COLUMN that runs idempotently in `_migrate_schema`.

**What did not change:**
- Pipeline behavior. TikTok analyze, photo support, bulk-analyze, creator sync, queue, web UI — all identical to v1.7.3.
- DB rows. Existing v1.7.3 DBs migrate in place with `media-archive check`. Existing rows get `platform='tiktok'` automatically via the column default.
- `creators.yaml` schema, `tags_vocabulary.example.yaml` schema, R2 bucket layout.
- Test count. 125/125 tests pass with no test changes — all the test imports were translated to the new package paths via the same sed pass that handled production code.

**What is now possible:**
- A YouTube plugin can land at `sources/youtube/` with its own ingest/process modules without touching any TikTok code.
- An HTTP API can land at `api/http/` and call into `core` + `sources` directly.
- An MCP server can land at `api/mcp/` and re-use the same underlying functions as the HTTP API.
- The audit log / scoped keys work (D-032+) sits cleanly between `api/` and `core/`.

**What was deferred:**
- Doc rewrite. `docs/architecture.md`, `docs/analyze.md`, `docs/queue.md`, etc. still describe the v1.7.x layout. These need a proper rewrite, but blowing through them in this scaffolding pass would be busywork. They get rewritten when Phase 2 milestones land that change behavior.
- TikTok export ingest. Cameron's TikTok export request is still pending; when it lands, the `media-archive ingest <export.zip>` path works identically to v1.7.3.

125/125 tests pass.

**Addendum (caught during v0.1.0 build):** PROJECT_ROOT in `core/config.py` originally walked up two parents (correct for v1.7.x where config.py sat at `src/tiktok_archive/config.py`). The reorg moved config.py one level deeper to `src/media_archive/core/config.py`, so PROJECT_ROOT now walks up four parents to reach the project root. Without this fix, pytest runs created stray `src/data/` directories because the resolved data dir landed inside the source tree. Caught and fixed pre-ship.

**Addendum (DB filename):** The default DB filename remains `tiktok.db` rather than `media.db` for v0.1.0. Two reasons: (1) zero-friction migration for v1.7.3 users — they `cp` their existing DB and it Just Works without renaming. (2) The DB file is platform-agnostic regardless of name; the `platform` column inside it does the actual classification. We can rename to `media.db` in a later release with a one-line `os.rename` migration if desired, but the cost/benefit isn't there yet.

---

## D-032 (v0.2.0): YouTube source plugin + chunked Whisper for long-form

**Decision:** Add a second source plugin (YouTube) under `sources/youtube/`. Move `transcribe.py` from `sources/tiktok/process/` to `core/transcribe/` since it's platform-agnostic. Add a new `core/transcribe/chunked.py` module that splits long audio into 20-minute chunks with 30-second overlap, transcribes each chunk independently with segment-level timestamps, and merges the results into a unified timeline. The CLI's `analyze` and `analyze-bulk` commands now dispatch to the right source plugin based on URL host.

**Why YouTube first (and not Instagram or local file):** TikTok and YouTube share yt-dlp as the underlying download tool, so the YouTube plugin reuses `sources/tiktok/ingest/downloader.py` directly. Long-form content (the v0.2.0 unlock) only really exists on YouTube — TikTok's max length is 60 minutes but practically 99% of content is under 5 min. Doing YouTube first gets us both the multi-source proof and the long-form payoff in a single release. Instagram requires its own auth flow (similar to the TikTok photo-post problem), and local file ingestion needs a metadata layer we haven't built yet.

**Why chunked Whisper, why now:** Whisper's quality and memory profile both degrade past ~30 minutes in a single pass. For a 2-hour podcast or lecture, naive single-pass transcription either OOMs or produces transcripts that drift mid-way through. Chunking with overlap is the standard solution — split into 20-min chunks (under the degradation threshold), give each chunk 30 seconds of overlap context with its neighbors so cross-boundary sentences get transcribed correctly, then dedupe the overlap region at merge time using Whisper's segment timestamps as the alignment key.

**Why not chunk everything:** For content under 25 minutes, the chunking overhead (extra ffmpeg calls, model re-init per chunk, segment merging) is pure cost with no benefit. The fast path delegates to the v1.x `transcribe_video_file` and wraps the result in a `ChunkedTranscript` with a single synthesized segment so downstream code has a uniform shape. This keeps short-content latency identical to v1.7.x.

**Tradeoffs in the YouTube analyzer:**
- The v0.2.0 YouTube `analyze.py` duplicates a lot of structure from the TikTok one (DB plumbing, status transitions, error reporting). We could have extracted a shared `analyze_engine.py` upfront, but doing it before having two real implementations would be premature abstraction. With both plugins in place now, the boundary becomes obvious and we can refactor cleanly in v0.3.0 or v0.4.0 — likely as part of the segments work since segment-aware analyze flow needs different behavior than the current single-transcript flow anyway.
- YouTube `author_handle` is left as `None` at insert time and populated later from yt-dlp metadata (`uploader_id` / `channel_id`). TikTok extracts the handle directly from the URL (`@handle/video/ID`), but YouTube URLs only have the channel ID/handle in the metadata, not the URL itself.
- Playlist URLs (`/playlist?list=...`) are detected via `is_playlist_url` but rejected at the validate stage. Playlist expansion is a v0.2.x point release. Watch URLs that include both `v=` and `list=` are treated as videos (the video wins).

**Schema changes:** None. The `platform` column added in v0.1.0 already covers YouTube content. New rows from the YouTube path get `platform="youtube"`.

**Bulk-analyze contract change:** The internal `_run_bulk_inline` and `_run_bulk_enqueue` functions now take `list[tuple[str, str]]` (platform, url) instead of `list[str]`. This is an internal API change — only the CLI and tests called these. The on-disk URL file format is unchanged (one URL per line, comments allowed); the CLI's bulk parser builds the tuples from the platform's `is_*_url` predicates.

**Worker dispatch:** The background worker (`media-archive worker`) reads platform off the Video row and dispatches to the right analyzer. v1.7.x workers don't have this column, but v0.1.0's migration filled in `platform="tiktok"` for existing rows so old jobs continue to work.

**Test count:** 168/168 pass (was 125; +27 YouTube URL tests, +9 chunked-transcribe unit tests, +5 multi-source bulk tests, -3 obsolete v1.7.x tests for "reject YouTube" behavior that v0.2.0 specifically reverses).

**What's still missing for v0.2.x point releases:**
- YouTube playlist expansion (`/playlist?list=...` → list of `/watch?v=...` URLs)
- YouTube channel sync (analog to the TikTok creator-sync pattern)
- Caption/subtitle ingestion (yt-dlp can fetch YouTube's auto-generated captions, which would let us skip Whisper entirely for content that already has high-quality captions — big speed win for long-form)
- Webapp UI for jump-to-timestamp (the segments are stored but the UI doesn't render them yet — that's v0.3.0)

**Locked-in tunables (configurable via env if needed later):**
- `SHORT_VIDEO_THRESHOLD_SEC = 1500` (25 min)
- `CHUNK_DURATION_SEC = 1200` (20 min)
- `CHUNK_OVERLAP_SEC = 30`

---

## D-032a (v0.2.1): YouTube analyzer list-column serialization hotfix

**Bug:** v0.2.0's YouTube analyzer crashed with `sqlite3.ProgrammingError: Error binding parameter 2: type 'list' is not supported` at the tag-write stage. Repro: `media-archive analyze "https://www.youtube.com/watch?v=dQw4w9WgXcQ"` — transcript and tag both succeeded, then SQLAlchemy choked on the UPDATE.

**Root cause:** `Video.key_points` and `Video.topics` are stored as JSON-encoded TEXT in SQLite (the schema doesn't use SQLAlchemy's typed JSON column — it stores serialized strings and the read path deserializes). The TikTok analyzer correctly wraps these with `json.dumps()` before assignment in three separate places (analyze_url, _analyze_photo_post, analyze_local_file). My v0.2.0 YouTube analyzer copied the assignment shape but missed the json.dumps wrapper, since the v0.2.0 tests mocked `analyze_url` rather than exercising the real DB write path.

**Fix:** Two-line patch — add `import json` and wrap both list-column assignments with `json.dumps(...)` to match the TikTok path's contract.

**Lesson for v0.3.0:** The list-column serialization is a real seam where the TikTok and YouTube analyzers have to do the same boilerplate, and missing it produces a runtime crash that no unit test caught. When we extract the shared analyze engine in v0.3.0 (alongside the segment table work), this assignment moves into the shared layer and only happens once — that's the right place to fix it permanently. For v0.2.x I'm keeping the fix local to the YouTube analyzer.

**Test coverage gap:** None of the v0.2.0 tests hit this code path because they all mock `analyze_url` itself. A proper integration test would pre-stub the downloader/whisper/tagger and let analyze_url touch the real (in-memory) DB. Filed as a v0.2.x cleanup task.

**Verified by Cam:** ProgrammingError reproduced cleanly on a Rick Roll URL (213.1s, single-pass path). The mixed bulk-analyze test also surfaced it on the second URL after the first TikTok succeeded — confirming the bug is in the YouTube tag-write specifically, not in the dispatcher.


---

## D-033 (v0.3.0): Collections + markdown export

**Decision:** Add a `Collection` and `CollectionMember` table pair, plus a CLI surface (`media-archive collection ...`) and a markdown/JSON/text export module. Collections are user-defined named groupings of analyzed posts; the export module serializes a collection to a single blob suitable for pasting into a Claude conversation. Compact export (summary + key points + topics + intent per post) is the default; `--full` adds transcripts.

**Why this instead of an MCP server:** Path B from the user — the goal is "make Claude able to reason across multiple posts at once" — could be solved either with an MCP server (Claude calls into the archive directly) or with a clipboard-friendly export (user pastes a curated chunk into the conversation). The MCP server is the longer build: schema definition, hosting, auth, protocol debugging, etc. Collections + export delivers 90% of the same outcome in one build session and produces a primitive (collections) that the eventual MCP server will sit on top of. When v0.5.0 lands and the MCP server exposes `get_collection(name)`, all the curation work the user did via this CLI carries forward unchanged.

**Why compact-default:** The user picked option 1 — compact summary-only by default, full transcripts behind `--full`. This is the right default because the typical use case is "drop the whole archive into a chat for cross-corpus reasoning," and full transcripts of 50+ posts blow past most context windows. Smoke test on 5 posts: compact = 1639 chars (~330/post), full = 2511 chars. At scale, that's roughly 16K vs 25K chars for 50 posts — well within budget for compact, getting tight for full.

**Schema decisions:**
- `Collection.name` is a unique string, not an opaque ID. The CLI uses the name as the addressable handle (`media-archive collection show offsec-notes`), which is friendlier than ints.
- `CollectionMember` has explicit `position` (integer, not timestamps) for ordering. Positions don't compact on remove — `max+1` always wins for new appends. This keeps adds O(1) and means the user can manually reorder later via the eventual webapp without having to compact gaps.
- `position` is `(max_pos or 0) + 1` rather than auto-increment so the user can later reorder via UPDATE without breaking the constraint.
- `CollectionMember` uses `ondelete="CASCADE"` on both `collection_id` and `video_id`. Deleting a collection unlinks its members; deleting a video drops it from any collection it was in. Videos otherwise survive `delete_collection` calls.
- `UniqueConstraint(collection_id, video_id)` makes idempotent adds enforceable at the DB level. The ops layer catches the IntegrityError and returns `{"added": False, "reason": "already_member"}` so the CLI can report cleanly.

**Why both `add-by-creator` and `add-by-topic`:** These are the two highest-value bulk-add patterns based on Cameron's archive content. Hand-adding 50 redtales90 stories one-by-one would be miserable. Topic-based add lets him build collections like "everything tagged 'security'" without having to remember individual URLs. Date-range add was also considered but cut for v0.3.0 — the user can add it as a v0.3.x point release if it turns out to be needed. The pattern is identical.

**Why three export formats (md/json/txt):**
- Markdown is the primary path. Renders well in Claude's context window. Easy for the user to preview before pasting.
- JSON is the fallback for programmatic consumers. The eventual MCP server will likely call `export_collection(format='json', full_transcripts=True)` internally.
- Text is markdown-stripped. Useful when piping to `less` or grep for shell workflows where markdown syntax is just noise.

**Test isolation hotfix:** While building this, several existing test files (test_bulk, test_parser, test_queue, test_media, test_sync) used `importlib.reload(schemas)` in their per-test fixtures. With the new `Collection`/`CollectionMember` tables that have a `relationship()` to `Video`, the reload pattern broke — reloading splits the SQLAlchemy class registry, leaving Video's `Mapped["Creator"]` relationship pointing at a stale Creator class. Fix: replace the reload with engine disposal + null-out of `_engine` and `_SessionLocal`. Same isolation guarantee, no registry damage. Applied to all five fixture files.

**Test count:** 210 passing, up from 168. +21 collection ops tests, +21 export tests. The fixture rewrite touched 5 files but didn't change any test behavior — all pre-existing tests continue to pass.

**Footer-as-prompt design:** The markdown export's final section is "## About this archive" — a short paragraph telling the LLM what the export is, that the entries are real curated content, and that it should not speculate beyond what's in the collection. This is essentially a system-prompt-by-paste, and it makes the export self-contained: the user doesn't need to write their own framing every time.

**What's deferred to v0.3.x:**
- Date-range bulk add (`add-by-date <start> <end>`)
- Webapp UI for collections (Collections tab with add/remove/export buttons)
- "Smart" collections that re-evaluate their query each time (vs the current static membership)
- Diff/merge between two collections
- Per-collection custom export templates

**What's locked in for v0.5.0:** When the MCP server lands, the first read tool will be `mcp_get_collection(name)` — same dict shape as `ops.show_collection()` — and the second will be `mcp_export_collection(name, full=False)` — same string output as the CLI's export. The MCP server is a thin protocol shim on top of the work done here.

