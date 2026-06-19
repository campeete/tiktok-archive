# HTTP API reference

All endpoints are served by the Flask app at `http://127.0.0.1:5050` by default. The host is bound to `127.0.0.1` only and is not reachable from other machines on the network. If you want to expose the UI, change `TT_WEB_HOST` and put a real auth layer in front of it.

## Pages (HTML)

| Method | Path                | Description                              |
| ------ | ------------------- | ---------------------------------------- |
| GET    | `/`                 | Home — analyze form + recent videos      |
| GET    | `/v/<video_id>`     | Video detail — transcript + summary + Q&A |
| GET    | `/creators`         | Creators list with sync buttons          |
| GET    | `/queue`            | Queue dashboard (auto-refreshes)         |

## JSON API

### `GET /api/health`

Returns server health.

```json
{
  "ok": true,
  "ollama": "OK",
  "storage": "local"
}
```

### `POST /api/analyze`

Analyze a single video. Multipart form fields:
- `url` (string): TikTok URL
- `file` (file): local video upload (max 200 MB)

Provide one or the other.

**Response:**
```json
{
  "ok": true,
  "video_id": 42,
  "url": "https://www.tiktok.com/@user/video/123",
  "transcript": "Hello and welcome...",
  "transcript_lang": "en",
  "summary": "...",
  "key_points": ["...", "..."],
  "topics": ["ai", "education"],
  "intent": "educate",
  "claim_check": false,
  "elapsed_sec": 47.3
}
```

**Error response:**
```json
{
  "ok": false,
  "stage": "download",
  "error": "...",
  "video_id": 42,
  "rate_limited": false
}
```

`stage` is one of `download`, `transcribe`, `tag`. `rate_limited` is `true` when TikTok returned a 429/403; the global throttle has been engaged.

### `POST /api/ask/<video_id>`

Ask a question about a previously analyzed video.

**Request:**
```json
{ "question": "what was the main argument?" }
```

**Response:**
```json
{ "ok": true, "answer": "..." }
```

The answer is grounded in the transcript only. If the question can't be answered from the transcript, the response is:
> "The transcript does not mention this."

### `GET /api/queue`

Queue stats + 20 most recent jobs. Polled every 5 seconds by `/queue` page.

**Response:**
```json
{
  "stats": {
    "by_status": { "pending": 5, "running": 1, "done": 234, "failed": 2 },
    "by_kind": {
      "download": { "pending": 5, "running": 1, "done": 234, "failed": 2 }
    },
    "recent_failures_24h": 2,
    "oldest_pending": "2024-01-15T12:34:56+00:00"
  },
  "recent": [
    {
      "id": 401,
      "kind": "download",
      "status": "running",
      "video_id": 142,
      "creator_id": 7,
      "attempts": 1,
      "last_error": null,
      "scheduled_for": "2024-01-15T12:34:00+00:00",
      "started_at": "2024-01-15T12:34:01+00:00",
      "finished_at": null,
      "duration_sec": null
    }
  ]
}
```

### `POST /api/sync/<handle>`

Enqueue a sync-creator job for one creator. Idempotently registers the creator if they don't exist yet.

**Response:**
```json
{ "ok": true, "creator_id": 7, "handle": "someuser" }
```

The actual sync happens when the worker picks up the job; this endpoint only schedules it. Make sure a worker is running.

## Error semantics

- 4xx: client errors (bad request, not found). Body is JSON with `error`.
- 5xx: server errors. Body is JSON with `error` if Flask caught it; raw HTML otherwise.

## CORS

There is none. The API is intended for the local web UI. If you need cross-origin access, add a CORS middleware in `webapp/app.py`.
