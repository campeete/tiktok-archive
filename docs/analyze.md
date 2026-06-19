# `analyze` — single-video pipeline

## CLI

```bash
tiktok-archive analyze <url-or-path> [--keep-video]
```

- `<url-or-path>`: a TikTok URL (video or photo post), or a path to a local video file.
- `--keep-video`: don't delete the `.mp4` after transcribe (default: delete).

Returns a JSON dict with `ok`, `video_id`, `transcript`, `summary`, `key_points`, `topics`, `intent`, `claim_check`, `elapsed_sec`.

## Photo posts

`tiktok-archive analyze https://www.tiktok.com/@user/photo/<id>` runs through a dedicated photo-post processor (added in v0.2.3). yt-dlp does not handle photo posts, so we fetch the post HTML directly, parse out image URLs and audio URL from the page's rehydration JSON, and process them ourselves.

Three possible transcript sources, in priority order:

1. **Audio voiceover.** Most photo posts in informational niches (commentary, tutorials, news) have a creator voiceover. The audio track is downloaded, ffmpeg extracts it, and Whisper transcribes it just like a video.
2. **Slide OCR (optional).** When audio is silent or music-only, and `tesseract` is installed (`brew install tesseract`), each slide image is downloaded to a tempdir, OCR'd, and the recovered text becomes the transcript. Each slide's text is annotated `[Slide N]` so multi-slide posts are easy to read. The image files themselves are deleted after OCR.
3. **Combined audio + OCR.** When the audio is too short to be useful (under 20 chars) but slides have text, both sources are concatenated.

If all three produce nothing — silent slideshow with no readable text — the pipeline writes a stub summary noting "Photo post with N slide(s) and no audio (silent slideshow)" and stores the row with image URLs preserved in metadata.

To enable OCR on slides:

```bash
brew install tesseract       # macOS
sudo apt install tesseract-ocr  # Linux
```

Then run `tiktok-archive check` — you should see `tesseract: OK (photo OCR enabled)` in the output.

To OCR slide text instead, you'd need a separate pass; that's a future phase.

## Bulk analysis

```bash
tiktok-archive analyze-bulk <urls-file>             # enqueue mode
tiktok-archive analyze-bulk <urls-file> --inline    # sequential foreground mode
```

The file format is one URL per line. Blank lines and lines starting with `#` are ignored. Inline comments after a URL (separated by ` #`) are also stripped. Duplicate URLs are deduplicated.

**Enqueue mode (default):** each URL becomes a `download` job in the queue. A worker process drains them. This is the right mode for batches >5 URLs because the worker handles rate-limit pauses and crashes gracefully. Idempotent — re-running the same file is safe; URLs already in the queue or already analyzed are skipped.

**Inline mode (`--inline`):** URLs are processed sequentially in the foreground and results are streamed to stdout. Best for small batches where you want to see each result as it completes.

Example workflow with the worker:

```bash
tiktok-archive analyze-bulk my-urls.txt
tiktok-archive worker          # let it run; drains everything
# ...later...
tiktok-archive stats           # see how many succeeded
```

## Web UI

Same single-video pipeline at `POST /api/analyze`. Form fields:
- `url` (string): TikTok URL
- `file` (multipart): local file upload (max 200 MB)

The form posts to the JSON API and renders the result in the page.

## Pipeline stages

| Stage       | Tool        | Failure mode                                      | Retried? |
| ----------- | ----------- | ------------------------------------------------- | -------- |
| Download    | yt-dlp      | 429/403 → rate-limit pause; network → 3 retries   | yes      |
| Audio       | ffmpeg      | missing ffmpeg → hard error; corrupt video → fail | no       |
| Transcribe  | Whisper     | OOM, missing model → hard error                   | yes      |
| Tag         | Ollama      | model not loaded → falls back to free-form        | yes      |
| Persist JSON| storage.put | R2 fail → warning logged, local copy still exists | no       |

If the video is already in the DB (matched on `url + source + collection_name`), the row is reused — no duplicate insertion. Stuck states (`downloading`, `failed`) are reset to `pending` automatically on re-analyze.

## Latency expectations (M-series, medium model)

| Source                      | Time    | Notes                              |
| --------------------------- | ------- | ---------------------------------- |
| 30s clip, cached Whisper    | 8-15s   | After first run                    |
| 30s clip, cold Whisper      | 90-120s | First run downloads ~1.5GB model   |
| 60s clip, cached Whisper    | 15-25s  |                                    |
| 3min clip                   | 40-60s  |                                    |

## Tag output schema

The Ollama tag prompt is constrained to JSON with these fields:

```json
{
  "summary": "one or two sentences",
  "key_points": ["bullet 1", "bullet 2", ...],
  "topics": ["ai", "cybersecurity", ...],
  "intent": "educate",
  "claim_check": true
}
```

- `topics` is constrained to slugs from `tags_vocabulary.yaml`. The model is told it cannot invent new topics; if no topics fit, it returns `[]`.
- `intent` is one of: `educate`, `entertain`, `promote`, `inform`, `persuade`, `vent`, `other`.
- `claim_check` is `true` when the transcript makes specific factual or numeric claims worth verifying.

## Q&A

`POST /api/ask/<video_id>` with `{"question": "..."}` returns `{"ok": true, "answer": "..."}`.

The QA prompt is hard-grounded: the model is instructed to answer ONLY from the transcript and to explicitly say "the transcript does not mention this" when the answer isn't present. This avoids hallucination but is not bulletproof; spot-check the answers.

CLI equivalent:
```bash
tiktok-archive ask 42 "what specific numbers were mentioned?"
```
