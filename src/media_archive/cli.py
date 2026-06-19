"""
Command-line interface for media-archive (formerly tiktok-archive v1.7.x).

Run `media-archive --help` for the top-level commands.
Run `media-archive <command> --help` for command-specific options.

Logging defaults to LOG_LEVEL from config; pass -v / -vv to override.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from media_archive.core import config
from media_archive.core.db.schemas import init_db


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(verbosity: int) -> None:
    level = config.LOG_LEVEL
    if verbosity >= 2:
        level = "DEBUG"
    elif verbosity == 1:
        level = "INFO"
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _print_table(rows: list[dict], headers: list[str]) -> None:
    """Minimal table printer. No external deps."""
    if not rows:
        print("(no rows)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, h in enumerate(headers):
            widths[i] = max(widths[i], len(str(row.get(h, ""))))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*[str(row.get(h, "")) for h in headers]))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    """Print environment diagnostics."""
    from media_archive.sources.tiktok.ingest.downloader import yt_dlp_available, yt_dlp_version
    from media_archive.sources.tiktok.process.photo import tesseract_available
    from media_archive.sources.tiktok.process.tag import ollama_available
    from media_archive.core.transcribe.transcribe import whisper_available
    from media_archive.core.storage import test_r2_connection

    diag = config.diagnostic_dict()
    print("=" * 60)
    print("media-archive environment")
    print("=" * 60)
    for k, v in diag.items():
        if isinstance(v, dict):
            print(f"  {k}:")
            for sk, sv in v.items():
                print(f"    {sk}: {sv}")
        else:
            print(f"  {k}: {v}")
    print("-" * 60)
    print("  yt-dlp:        ", yt_dlp_version() if yt_dlp_available() else "(not installed)")
    ok, info = whisper_available()
    print(f"  whisper:        {'OK ' + info if ok else 'MISSING — ' + info}")
    ok, msg = ollama_available()
    print(f"  ollama:         {'OK' if ok else 'MISSING — ' + msg}")
    if tesseract_available():
        print(f"  tesseract:      OK (photo OCR enabled)")
    else:
        print(f"  tesseract:      not installed (photo OCR disabled — `brew install tesseract` to enable)")
    from media_archive.sources.tiktok.process.browser_fetch import is_browser_available, _have_saved_profile
    if is_browser_available():
        if _have_saved_profile():
            print(f"  playwright:     OK (photo posts via real browser, session saved)")
        else:
            print(f"  playwright:     OK (no saved session — photo posts likely to fail; run `media-archive auth-tiktok`)")
    else:
        print(f"  playwright:     not installed (photo posts will fail — `pip install playwright && playwright install chromium`)")
    from media_archive.sources.tiktok.process.media import _pillow_available, _scenedetect_available
    if _pillow_available():
        print(f"  Pillow:         OK (thumbnails enabled)")
    else:
        print(f"  Pillow:         not installed (no thumbnails — `pip install -e \".[media]\"` to enable)")
    if _scenedetect_available():
        print(f"  scenedetect:    OK (scene-change frames enabled)")
    else:
        print(f"  scenedetect:    not installed (uniform thumbs only — `pip install -e \".[media]\"` to enable)")
    if config.STORAGE_BACKEND == "r2":
        ok, msg = test_r2_connection()
        print(f"  r2:             {'OK — ' + msg if ok else 'FAIL — ' + msg}")
    print("=" * 60)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from media_archive.sources.tiktok.ingest.parser import ingest_export

    path = Path(args.path).expanduser().resolve()
    result = ingest_export(path)
    print(json.dumps(result, indent=2))
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    """Drain pending download jobs (legacy bulk command)."""
    from media_archive.core.queue.worker import run

    n = run(once=True, max_jobs=args.limit)
    print(f"Processed {n} jobs.")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    """Same as download — kept for backward-compat. Worker handles all stages."""
    return cmd_download(args)


def cmd_tag(args: argparse.Namespace) -> int:
    """Re-tag any analyzed videos that don't have summaries (Phase 1.7+).

    For now, behaves like download: drains the queue.
    """
    return cmd_download(args)


def cmd_embed(args: argparse.Namespace) -> int:
    from media_archive.core.index.embed import embed_pending

    n = embed_pending(limit=args.limit)
    print(f"Embedded {n} videos. (Phase 5 not yet wired)")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from media_archive.core.index.embed import search

    results = search(args.query, k=args.k)
    if not results:
        print("(no results — Phase 5 search not yet implemented)")
        return 0
    print(json.dumps(results, indent=2))
    return 0


def cmd_auth_tiktok(args: argparse.Namespace) -> int:
    """One-time login flow that saves a TikTok session for the browser
    fetcher. After this, photo posts can be fetched authenticated."""
    from media_archive.sources.tiktok.process.browser_fetch import launch_auth_session
    ok = launch_auth_session()
    return 0 if ok else 1


def cmd_debug_photo(args: argparse.Namespace) -> int:
    """Diagnostic: fetch a photo post URL and report what we got.

    Tries both the plain requests fetcher and the Playwright browser
    fetcher, so the user can see which one (if either) is returning
    real post data.
    """
    from media_archive.sources.tiktok.process import photo as photo_module

    url = args.url

    def _report(html: str, label: str) -> bool:
        """Print diagnostics for an HTML blob. Returns True if item found."""
        print(f"\n=== {label} ===")
        print(f"Length: {len(html)} chars")
        print(f"Has __UNIVERSAL_DATA_FOR_REHYDRATION__: {'__UNIVERSAL_DATA_FOR_REHYDRATION__' in html}")
        print(f"Has SIGI_STATE: {'SIGI_STATE' in html}")
        print(f"Has __NEXT_DATA__: {'__NEXT_DATA__' in html}")
        print(f"Has captcha-related: {any(s in html.lower() for s in ['captcha', 'verify', 'robot'])}")
        print(f"Has login redirect: {'tiktok.com/login' in html}")

        import re as _re
        ids = _re.findall(r'<script[^>]+id="([^"]+)"', html)
        print(f"Script tag IDs found ({len(ids)}):")
        for i in ids[:30]:
            print(f"  - {i}")

        data = photo_module._extract_rehydration_json(html)
        if data:
            print(f"Rehydration JSON parsed. Top-level keys: {list(data.keys())[:10]}")
            item = photo_module._walk_to_item(data)
            if item:
                print(f"Item dict FOUND. id={item.get('id')}")
                print(f"  imagePost present: {'imagePost' in item}")
                print(f"  music present: {'music' in item}")
                print(f"  desc: {(item.get('desc') or '')[:100]}")
                ip = item.get("imagePost", {})
                if isinstance(ip, dict):
                    imgs = ip.get("images", [])
                    print(f"  image count: {len(imgs) if isinstance(imgs, list) else '?'}")
                return True
            else:
                print("Item NOT FOUND — TikTok shell page only.")
                scope = data.get("__DEFAULT_SCOPE__", {})
                if isinstance(scope, dict):
                    print(f"Scope keys: {list(scope.keys())[:20]}")
        else:
            print("No rehydration JSON found.")
        return False

    # 1) Plain requests fetch
    print(f"Fetching: {url}")
    try:
        html = photo_module._fetch_post_html(url)
        plain_ok = _report(html, "PLAIN REQUESTS")
    except Exception as e:
        print(f"PLAIN REQUESTS failed: {type(e).__name__}: {e}")
        plain_ok = False

    if plain_ok:
        print("\nPlain requests fetch succeeds — no need for browser fallback.")
        return 0

    # 2) Browser fetch
    print("\nPlain fetch did not return post data. Trying browser fetch...")
    from media_archive.sources.tiktok.process import browser_fetch
    try:
        html = browser_fetch.fetch_with_browser(url)
        browser_ok = _report(html, "BROWSER (Playwright)")
        if browser_ok:
            print("\nBrowser fetch succeeds — analyze should work.")
            return 0
        else:
            print("\nBrowser fetch returned a page but item data is missing.")
            return 1
    except browser_fetch.BrowserFetchUnavailable as e:
        print(f"\nBROWSER UNAVAILABLE: {e}")
        print("Install with: pip install playwright && playwright install chromium")
        return 1
    except browser_fetch.BrowserFetchError as e:
        print(f"\nBROWSER FETCH ERROR: {e}")
        return 1


def cmd_analyze(args: argparse.Namespace) -> int:
    """Analyze one URL or local file. Synchronous.

    Dispatches to the right source plugin based on URL host:
      - tiktok.com / vm.tiktok.com / etc.   → sources.tiktok
      - youtube.com / youtu.be / etc.       → sources.youtube
      - any other URL                       → reject with stage=validate
      - local file path                     → sources.tiktok analyze_local_file
        (single-pass; long-form local files via youtube path is a v0.3.0 task)
    """
    from media_archive.sources.tiktok.ingest.urls import is_tiktok_url
    from media_archive.sources.youtube.ingest.urls import is_youtube_url

    target = args.target
    if target.startswith(("http://", "https://")):
        if is_tiktok_url(target):
            from media_archive.sources.tiktok.process.analyze import analyze_url as tt_analyze
            result = tt_analyze(target, keep_video=args.keep_video)
        elif is_youtube_url(target):
            from media_archive.sources.youtube.process.analyze import analyze_url as yt_analyze
            result = yt_analyze(target, keep_video=args.keep_video)
        else:
            # Unknown host. Future source plugins (Instagram, local file
            # URL list, etc.) get their own branches here as they land.
            result = {
                "ok": False,
                "stage": "validate",
                "error": (
                    f"Unsupported URL: {target}. "
                    "media-archive currently supports tiktok.com and youtube.com URLs."
                ),
            }
            print(json.dumps(result, indent=2, default=str))
            return 1
    else:
        # Local file path. For now, route through the TikTok analyze_local_file
        # since it's the only source with that entry point. v0.3.0 adds a
        # neutral local-file analyzer that picks chunked vs single-pass
        # based on duration.
        from media_archive.sources.tiktok.process.analyze import analyze_local_file
        path = Path(target).expanduser().resolve()
        if not path.is_file():
            print(f"Not a URL or existing file: {target}", file=sys.stderr)
            return 2
        result = analyze_local_file(path)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


def cmd_analyze_bulk(args: argparse.Namespace) -> int:
    """Analyze every URL in a file.

    File format: one URL per line. Blank lines and lines beginning with '#'
    are ignored. Duplicate URLs are deduplicated.

    Accepts both TikTok and YouTube URLs (mixed in the same file is fine).
    Each URL is normalized through its platform's URL utility, then queued
    or run inline. URLs from unsupported hosts are reported and skipped.

    By default, URLs are enqueued as 'download' jobs and a separate worker
    drains them. With --inline, the URLs are processed sequentially in the
    foreground and results are printed as they complete.
    """
    from media_archive.core.db.schemas import Video, get_session, init_db
    from media_archive.sources.tiktok.ingest.urls import (
        is_tiktok_url, normalize_tiktok_url,
    )
    from media_archive.sources.youtube.ingest.urls import (
        is_youtube_url, normalize_youtube_url,
    )

    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 2

    # Per-URL parse: classify by host, normalize through the right utility,
    # bucket into (platform, url) pairs. Unknown hosts and unparseable lines
    # are tracked separately for end-of-parse reporting.
    classified: list[tuple[str, str]] = []  # [(platform, url), ...]
    rejected_unsupported: list[str] = []
    rejected_unparseable: list[str] = []

    with path.open() as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            # Allow trailing comments on a URL line
            if " #" in s:
                s = s.split(" #", 1)[0].strip()
            if not s.startswith(("http://", "https://")):
                rejected_unparseable.append(s)
                continue
            if is_tiktok_url(s):
                classified.append(("tiktok", normalize_tiktok_url(s)))
            elif is_youtube_url(s):
                classified.append(("youtube", normalize_youtube_url(s)))
            else:
                rejected_unsupported.append(s)

    if rejected_unsupported:
        print(
            f"Skipped {len(rejected_unsupported)} unsupported URL(s) "
            f"(media-archive supports tiktok.com and youtube.com):"
        )
        for url in rejected_unsupported[:5]:
            print(f"  - {url}")
        if len(rejected_unsupported) > 5:
            print(f"  ... and {len(rejected_unsupported) - 5} more")

    if rejected_unparseable:
        print(
            f"Skipped {len(rejected_unparseable)} unparseable line(s) "
            f"(not a URL):"
        )
        for line_text in rejected_unparseable[:3]:
            print(f"  - {line_text[:80]}")
        if len(rejected_unparseable) > 3:
            print(f"  ... and {len(rejected_unparseable) - 3} more")

    if not classified:
        print("No valid URLs found in the file.", file=sys.stderr)
        return 1

    # Dedupe by URL (keep first platform classification per URL).
    seen: set[str] = set()
    urls: list[tuple[str, str]] = []
    for platform, u in classified:
        if u not in seen:
            seen.add(u)
            urls.append((platform, u))

    # Per-platform tally for the user.
    counts: dict[str, int] = {}
    for platform, _ in urls:
        counts[platform] = counts.get(platform, 0) + 1
    counts_str = ", ".join(f"{n} {p}" for p, n in sorted(counts.items()))
    print(f"Found {len(urls)} URLs ({counts_str}) in {path}.")

    if args.inline:
        return _run_bulk_inline(urls, keep_video=args.keep_video)
    else:
        return _run_bulk_enqueue(urls)


def _run_bulk_inline(urls: list[tuple[str, str]], *, keep_video: bool = False) -> int:
    """Run URLs sequentially in the foreground, printing results.

    Each entry is (platform, url). The runner imports the right analyzer
    lazily so that test environments without youtube deps don't crash on
    a TikTok-only file (and vice versa).
    """
    ok_count = 0
    fail_count = 0
    for i, (platform, url) in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] [{platform}] {url}")
        try:
            if platform == "tiktok":
                from media_archive.sources.tiktok.process.analyze import analyze_url
            elif platform == "youtube":
                from media_archive.sources.youtube.process.analyze import analyze_url
            else:
                fail_count += 1
                print(f"  FAIL at validate — Unknown platform: {platform}")
                continue
            result = analyze_url(url, keep_video=keep_video)
        except Exception as e:
            fail_count += 1
            print(f"  CRASH: {type(e).__name__}: {e}")
            continue
        if result.get("ok"):
            ok_count += 1
            elapsed = result.get("elapsed_sec") or 0
            summary = (result.get("summary") or "").replace("\n", " ")
            if len(summary) > 90:
                summary = summary[:87] + "..."
            chunk_info = ""
            if result.get("chunk_count", 1) > 1:
                chunk_info = f" [{result['chunk_count']} chunks]"
            print(f"  OK in {elapsed:.1f}s{chunk_info} — {summary}")
        else:
            fail_count += 1
            stage = result.get("stage") or "?"
            err = (result.get("error") or "")[:200]
            rate_limited = result.get("rate_limited")
            tag = " [RATE-LIMITED]" if rate_limited else ""
            print(f"  FAIL at {stage}{tag} — {err}")

    print(f"\nDone. {ok_count} succeeded, {fail_count} failed.")
    return 0 if fail_count == 0 else 1


def _run_bulk_enqueue(urls: list[tuple[str, str]]) -> int:
    """Insert each URL as a Video row + 'download' job, ready for a worker.

    Each entry is (platform, url). The worker reads platform off the Video
    row and dispatches to the right analyzer.

    Idempotent: skips URLs that are already analyzed (tagged_at IS NOT NULL)
    or that already have a pending/running download job in the queue.
    """
    from media_archive.core.db.schemas import Job, Video, get_session, init_db
    from media_archive.sources.tiktok.ingest.urls import (
        extract_handle as tt_handle, extract_video_id as tt_vid,
    )
    from media_archive.sources.youtube.ingest.urls import (
        extract_video_id as yt_vid,
    )
    from media_archive.core.queue import enqueue
    from sqlalchemy.exc import IntegrityError

    init_db()
    session = get_session()
    enqueued = 0
    skipped = 0
    try:
        for platform, url in urls:
            existing = (
                session.query(Video)
                .filter(
                    Video.url == url,
                    Video.source == "bulk",
                    Video.collection_name == "",
                )
                .one_or_none()
            )
            if existing is None:
                # Per-platform extraction. YouTube doesn't have a handle in
                # the URL itself (it's a channel ID resolved at download time
                # via yt-dlp metadata), so we leave author_handle=None for
                # YouTube rows and let _populate_metadata fill it in.
                if platform == "tiktok":
                    extracted_id = tt_vid(url)
                    extracted_handle = tt_handle(url)
                elif platform == "youtube":
                    extracted_id = yt_vid(url)
                    extracted_handle = None
                else:
                    skipped += 1
                    continue

                video = Video(
                    url=url,
                    source="bulk",
                    collection_name="",
                    platform=platform,
                    video_id=extracted_id,
                    author_handle=extracted_handle,
                    download_status="pending",
                )
                session.add(video)
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    skipped += 1
                    continue
                video_id = video.id
            else:
                if existing.tagged_at is not None:
                    skipped += 1
                    continue
                active_job = (
                    session.query(Job)
                    .filter(
                        Job.video_id == existing.id,
                        Job.kind == "download",
                        Job.status.in_(["pending", "running"]),
                    )
                    .first()
                )
                if active_job is not None:
                    skipped += 1
                    continue
                video_id = existing.id

            enqueue("download", video_id=video_id, session=session)
            enqueued += 1
        session.commit()
    finally:
        session.close()

    print(f"Enqueued {enqueued} jobs. Skipped {skipped} already-analyzed or already-queued URLs.")
    print("\nStart a worker to drain:")
    print("  media-archive worker          # forever")
    print("  media-archive worker --once   # drain and exit")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Ask a question about a previously analyzed video."""
    from media_archive.core.db.schemas import Video, get_session
    from media_archive.sources.tiktok.process.qa import ask

    init_db()
    session = get_session()
    try:
        video = session.get(Video, args.video_id)
        if video is None:
            print(f"No video with id {args.video_id}", file=sys.stderr)
            return 2
        if not video.transcript:
            print(f"Video {args.video_id} has no transcript yet", file=sys.stderr)
            return 2
        transcript = video.transcript
    finally:
        session.close()

    answer = ask(transcript, args.question)
    print(answer)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the local web UI."""
    try:
        from media_archive.core.webapp.app import create_app
    except ImportError as e:
        print(f"Web UI deps missing. Install with: pip install -e '.[web]'\n  ({e})", file=sys.stderr)
        return 2

    app = create_app()
    print()
    print(f"  media-archive web UI starting on http://{config.WEB_HOST}:{config.WEB_PORT}")
    print(f"  data dir:     {config.DATA_DIR}")
    print(f"  ollama host:  {config.OLLAMA_HOST}")
    print(f"  whisper:      {config.WHISPER_MODEL}")
    print(f"  tag model:    {config.TAG_MODEL}")
    print(f"  storage:      {config.STORAGE_BACKEND}{' (R2 mirror)' if config.r2_configured() else ''}")
    print()
    print("  Press Ctrl+C to stop.")
    print()
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False, use_reloader=False)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Pipeline state summary."""
    from media_archive.core.db.schemas import Creator, Job, Video, get_session
    from media_archive.core.queue import queue_stats
    from sqlalchemy import func

    init_db()
    session = get_session()
    try:
        # Video counts
        total = session.query(func.count(Video.id)).scalar() or 0
        by_status = dict(
            session.query(Video.download_status, func.count(Video.id)).group_by(Video.download_status).all()
        )
        transcribed = session.query(func.count(Video.id)).filter(Video.transcribed_at.isnot(None)).scalar() or 0
        tagged = session.query(func.count(Video.id)).filter(Video.tagged_at.isnot(None)).scalar() or 0
        creators = session.query(func.count(Creator.id)).scalar() or 0
        active_creators = session.query(func.count(Creator.id)).filter(Creator.enabled.is_(True)).scalar() or 0
    finally:
        session.close()

    qstats = queue_stats()

    print("=" * 60)
    print("media-archive stats")
    print("=" * 60)
    print(f"  Videos total:   {total}")
    print(f"  Transcribed:    {transcribed}")
    print(f"  Tagged:         {tagged}")
    print(f"  By status:")
    for status, count in sorted(by_status.items()):
        print(f"    {status:<14} {count}")
    print()
    print(f"  Creators:       {creators} ({active_creators} enabled)")
    print()
    print(f"  Jobs by status: {qstats.get('by_status') or '(empty)'}")
    print(f"  Jobs by kind:")
    for kind, kstats in (qstats.get("by_kind") or {}).items():
        print(f"    {kind:<14} {kstats}")
    print(f"  Recent failures (24h): {qstats.get('recent_failures_24h', 0)}")
    print(f"  Oldest pending:       {qstats.get('oldest_pending') or '(none)'}")
    print("=" * 60)
    return 0


# ---------------------------------------------------------------------------
# Creator subcommands
# ---------------------------------------------------------------------------

def cmd_creator(args: argparse.Namespace) -> int:
    sub = args.creator_action
    if sub == "add":
        from media_archive.sources.tiktok.sync import add_creator
        creator = add_creator(args.handle, sync_depth=args.sync_depth, notes=args.notes)
        print(f"Added creator @{creator.handle} (id={creator.id})")
        return 0
    if sub == "list":
        from media_archive.sources.tiktok.sync import list_creators
        creators = list_creators()
        rows = [
            {
                "handle": c.handle,
                "enabled": "Y" if c.enabled else "N",
                "depth": c.sync_depth,
                "last_synced": c.last_synced_at.isoformat() if c.last_synced_at else "(never)",
                "notes": (c.notes or "")[:40],
            }
            for c in creators
        ]
        _print_table(rows, ["handle", "enabled", "depth", "last_synced", "notes"])
        return 0
    if sub == "sync":
        from media_archive.sources.tiktok.sync import sync_all, sync_creator
        if args.handle and args.handle != "--all":
            result = sync_creator(args.handle)
            print(json.dumps(result, indent=2))
        else:
            results = sync_all(only_due=not args.force)
            print(json.dumps(results, indent=2))
        return 0
    if sub == "disable":
        from media_archive.sources.tiktok.sync import disable_creator
        ok = disable_creator(args.handle)
        print("Disabled" if ok else "Not found")
        return 0
    if sub == "remove":
        from media_archive.sources.tiktok.sync import remove_creator
        ok = remove_creator(args.handle)
        print("Removed" if ok else "Not found")
        return 0
    if sub == "import-from-export":
        from media_archive.sources.tiktok.sync import import_from_export
        path = Path(args.path).expanduser().resolve()
        result = import_from_export(path, min_video_count=args.min_videos)
        print(json.dumps(result, indent=2))
        return 0
    if sub == "import-from-yaml":
        from media_archive.sources.tiktok.sync import import_from_yaml
        path = Path(args.path).expanduser().resolve() if args.path else None
        result = import_from_yaml(path)
        print(json.dumps(result, indent=2))
        return 0
    if sub == "export-to-yaml":
        from media_archive.sources.tiktok.sync import export_to_yaml
        path = Path(args.path).expanduser().resolve() if args.path else None
        out = export_to_yaml(path)
        print(f"Wrote {out}")
        return 0
    print(f"Unknown creator action: {sub}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Collection subcommand (v0.3.0)
# ---------------------------------------------------------------------------

def cmd_collection(args: argparse.Namespace) -> int:
    """Dispatcher for `media-archive collection <action>`.

    All sub-actions share this entry point; the specific action is
    args.collection_action. We import collection ops lazily so a stale
    or partially-installed v0.2.x environment without the new module
    still loads the rest of the CLI.
    """
    from media_archive.core.collections import ops, export
    from media_archive.core.collections.ops import (
        CollectionAlreadyExistsError,
        CollectionError,
        CollectionNotFoundError,
    )

    sub = args.collection_action

    # ---- create ----
    if sub == "create":
        try:
            result = ops.create_collection(args.name, description=args.description)
        except CollectionAlreadyExistsError as e:
            print(str(e), file=sys.stderr)
            return 1
        except CollectionError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"Created collection {result['name']!r} (id={result['id']}).")
        return 0

    # ---- list ----
    if sub == "list":
        items = ops.list_collections()
        if not items:
            print("No collections yet. Create one with: media-archive collection create <name>")
            return 0
        # Aligned table-ish output. We avoid external deps; just pad columns.
        max_name = max(len(c["name"]) for c in items)
        print(f"{'NAME'.ljust(max_name)}  COUNT  DESCRIPTION")
        for c in items:
            desc = (c["description"] or "")[:60]
            print(f"{c['name'].ljust(max_name)}  {str(c['member_count']).rjust(5)}  {desc}")
        return 0

    # ---- show ----
    if sub == "show":
        try:
            data = ops.show_collection(args.name)
        except CollectionNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"Collection: {data['name']}")
        if data.get("description"):
            print(f"  {data['description']}")
        print(f"  Members: {data['member_count']}")
        if data["member_count"] == 0:
            print("  (empty — use `collection add` to populate)")
            return 0
        print()
        for m in data["members"]:
            handle = m.get("author_handle") or "unknown"
            platform = m.get("platform") or "tiktok"
            summary = (m.get("summary") or "(no summary)").replace("\n", " ")
            if len(summary) > 80:
                summary = summary[:77] + "..."
            print(f"  [{m.get('position'):>3}] [{platform}] @{handle}: {summary}")
        return 0

    # ---- add ----
    if sub == "add":
        try:
            ops._get_collection_by_name  # type: ignore  # quick existence check
            # fall through; we use the public add method per target
        except AttributeError:
            pass
        added = 0
        skipped = 0
        not_found = 0
        for target in args.targets:
            try:
                result = ops.add_video_to_collection(
                    args.name, target, note=getattr(args, "note", None),
                )
            except CollectionNotFoundError as e:
                print(str(e), file=sys.stderr)
                return 1
            if result.get("added"):
                added += 1
                print(f"  + added {target} (video_id={result['video_id']}, position={result['position']})")
            else:
                reason = result.get("reason")
                if reason == "already_member":
                    skipped += 1
                    print(f"  · already in collection: {target}")
                elif reason == "video_not_found":
                    not_found += 1
                    print(f"  ! video not found: {target}", file=sys.stderr)
                else:
                    print(f"  ? {target}: {reason}")
        print(f"\n{added} added, {skipped} already-member, {not_found} not-found.")
        return 0 if not_found == 0 else 1

    # ---- remove ----
    if sub == "remove":
        removed = 0
        skipped = 0
        for target in args.targets:
            try:
                result = ops.remove_video_from_collection(args.name, target)
            except CollectionNotFoundError as e:
                print(str(e), file=sys.stderr)
                return 1
            if result.get("removed"):
                removed += 1
                print(f"  - removed {target}")
            else:
                skipped += 1
                print(f"  · {target}: {result.get('reason')}")
        print(f"\n{removed} removed, {skipped} skipped.")
        return 0

    # ---- delete ----
    if sub == "delete":
        if not getattr(args, "yes", False):
            try:
                resp = input(f"Delete collection {args.name!r}? [y/N] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                return 1
            if resp not in ("y", "yes"):
                print("Cancelled.")
                return 1
        try:
            removed = ops.delete_collection(args.name)
        except CollectionNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"Deleted collection {args.name!r} ({removed} member(s) unlinked, videos retained).")
        return 0

    # ---- add-by-creator ----
    if sub == "add-by-creator":
        try:
            result = ops.add_by_creator(args.name, args.handle)
        except CollectionNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(
            f"Matched {result.get('matched', 0)} video(s) by @{args.handle.lstrip('@')}, "
            f"added {result['added']}, skipped {result['skipped']} already-member."
        )
        return 0

    # ---- add-by-topic ----
    if sub == "add-by-topic":
        try:
            result = ops.add_by_topic(args.name, args.topic)
        except CollectionNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 1
        if result.get("error"):
            print(f"Error: {result['error']}", file=sys.stderr)
            return 2
        print(
            f"Added {result['added']} video(s) tagged with topic {args.topic!r}, "
            f"skipped {result['skipped']} already-member."
        )
        return 0

    # ---- export ----
    if sub == "export":
        try:
            data = ops.show_collection(args.name)
        except CollectionNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 1
        rendered = export.export_collection(
            data,
            format=args.format,
            full_transcripts=args.full,
            transcript_max_words=args.max_words,
        )
        if args.out:
            out_path = Path(args.out).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered, encoding="utf-8")
            print(f"Wrote {len(rendered):,} chars to {out_path}", file=sys.stderr)
        else:
            print(rendered)
        return 0

    print(f"Unknown collection action: {sub}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Worker subcommand
# ---------------------------------------------------------------------------

def cmd_worker(args: argparse.Namespace) -> int:
    from media_archive.core.queue.worker import run

    n = run(once=args.once, max_jobs=args.max_jobs)
    if args.once:
        print(f"Processed {n} jobs.")
    return 0


# ---------------------------------------------------------------------------
# R2 setup
# ---------------------------------------------------------------------------

def cmd_setup_r2(args: argparse.Namespace) -> int:
    """Test the configured R2 connection."""
    from media_archive.core.storage import test_r2_connection

    if config.STORAGE_BACKEND != "r2":
        print(
            "STORAGE_BACKEND is not 'r2'. Set TT_STORAGE_BACKEND=r2 in .env "
            "and configure R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, "
            "R2_BUCKET_NAME."
        )
        return 1
    ok, msg = test_r2_connection()
    print(("✓ " if ok else "✗ ") + msg)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# DB backup
# ---------------------------------------------------------------------------

def cmd_backup_db(args: argparse.Namespace) -> int:
    """Snapshot the SQLite DB to local + R2 (if configured)."""
    import datetime as _dt
    import shutil

    from media_archive.core.storage import make_storage

    init_db()
    config.ensure_dirs()
    timestamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = config.DB_BACKUPS_DIR / f"tiktok-{timestamp}.db"
    # SQLite is safe to copy under WAL as long as both files come along; for a
    # quick snapshot we use the .backup API via sqlite3 shell when possible.
    src = config.DB_PATH
    if not src.exists():
        print(f"DB not found: {src}", file=sys.stderr)
        return 1

    try:
        import sqlite3
        with sqlite3.connect(str(src)) as src_conn, sqlite3.connect(str(out_path)) as dest_conn:
            src_conn.backup(dest_conn)
    except Exception:
        # Fallback: plain copy
        shutil.copy2(src, out_path)

    print(f"Local backup: {out_path}")

    if config.STORAGE_BACKEND == "r2" and config.r2_configured():
        storage = make_storage()
        key = f"db-backups/tiktok-{timestamp}.db"
        try:
            storage.put(key, out_path.read_bytes())
            print(f"R2 backup:    {key}")
        except Exception as e:
            print(f"R2 upload failed: {e}", file=sys.stderr)
            return 1

    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="media-archive")
    p.add_argument("-v", "--verbose", action="count", default=0)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("check", help="Print environment diagnostics")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("ingest", help="Parse a TikTok export ZIP/JSON into the DB")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("download", help="Drain pending download jobs (calls worker)")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("transcribe", help="(alias for download)")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_transcribe)

    sp = sub.add_parser("tag", help="(alias for download)")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_tag)

    sp = sub.add_parser("embed", help="Embed tagged videos into vector store (Phase 5)")
    sp.add_argument("--limit", type=int, default=100)
    sp.set_defaults(func=cmd_embed)

    sp = sub.add_parser("search", help="Semantic search (Phase 5)")
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=10)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("analyze", help="Analyze one URL or file (transcript + summary + tags)")
    sp.add_argument("target", help="URL or path to local video file")
    sp.add_argument("--keep-video", action="store_true", help="Don't delete the .mp4 after transcribe")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser(
        "debug-photo",
        help="Fetch a TikTok photo post URL and dump diagnostics about the page structure",
    )
    sp.add_argument("url", help="TikTok /photo/ URL")
    sp.set_defaults(func=cmd_debug_photo)

    sp = sub.add_parser(
        "auth-tiktok",
        help="One-time interactive login that saves a TikTok session for photo-post fetches",
    )
    sp.set_defaults(func=cmd_auth_tiktok)

    sp = sub.add_parser(
        "analyze-bulk",
        help="Analyze every URL in a file (one per line; '#' comments allowed)",
    )
    sp.add_argument("file", help="Path to a text file containing TikTok URLs")
    sp.add_argument(
        "--inline",
        action="store_true",
        help="Process URLs sequentially in the foreground instead of enqueueing for a worker",
    )
    sp.add_argument(
        "--keep-video",
        action="store_true",
        help="Don't delete the .mp4 after transcribe (only meaningful with --inline)",
    )
    sp.set_defaults(func=cmd_analyze_bulk)

    sp = sub.add_parser("ask", help="Ask a question about a previously analyzed video")
    sp.add_argument("video_id", type=int)
    sp.add_argument("question")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("serve", help="Start the local web UI")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("stats", help="Pipeline state summary")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("creator", help="Manage followed creators")
    csub = sp.add_subparsers(dest="creator_action", required=True)

    cs = csub.add_parser("add", help="Register a new creator")
    cs.add_argument("handle")
    cs.add_argument("--sync-depth", choices=["full", "last-6mo", "last-50"])
    cs.add_argument("--notes")
    cs.set_defaults(func=cmd_creator)

    cs = csub.add_parser("list", help="List all registered creators")
    cs.set_defaults(func=cmd_creator)

    cs = csub.add_parser("sync", help="Sync one creator (or --all)")
    cs.add_argument("handle", nargs="?", default="--all", help="@handle, or omit for all")
    cs.add_argument("--force", action="store_true", help="Sync even if recently synced")
    cs.set_defaults(func=cmd_creator)

    cs = csub.add_parser("disable", help="Stop syncing a creator (keeps history)")
    cs.add_argument("handle")
    cs.set_defaults(func=cmd_creator)

    cs = csub.add_parser("remove", help="Permanently remove a creator from sync list")
    cs.add_argument("handle")
    cs.set_defaults(func=cmd_creator)

    cs = csub.add_parser("import-from-export", help="Seed creators.yaml from a TikTok export")
    cs.add_argument("path")
    cs.add_argument("--min-videos", type=int, default=1, help="Only add creators with at least N videos in your export")
    cs.set_defaults(func=cmd_creator)

    cs = csub.add_parser("import-from-yaml", help="Sync creators.yaml -> DB")
    cs.add_argument("path", nargs="?")
    cs.set_defaults(func=cmd_creator)

    cs = csub.add_parser("export-to-yaml", help="Dump current creators table -> creators.yaml")
    cs.add_argument("path", nargs="?")
    cs.set_defaults(func=cmd_creator)

    sp = sub.add_parser("collection", help="Manage collections (groups of analyzed posts)")
    cosub = sp.add_subparsers(dest="collection_action", required=True)

    co = cosub.add_parser("create", help="Create a new empty collection")
    co.add_argument("name", help="Unique short name for the collection")
    co.add_argument("--description", "-d", help="Optional description")
    co.set_defaults(func=cmd_collection)

    co = cosub.add_parser("list", help="List all collections")
    co.set_defaults(func=cmd_collection)

    co = cosub.add_parser("show", help="Show a collection's members")
    co.add_argument("name")
    co.set_defaults(func=cmd_collection)

    co = cosub.add_parser("add", help="Add one or more videos (by URL or DB id) to a collection")
    co.add_argument("name", help="Collection name")
    co.add_argument("targets", nargs="+", help="URLs or numeric video IDs to add")
    co.add_argument("--note", help="Optional note attached to the membership row")
    co.set_defaults(func=cmd_collection)

    co = cosub.add_parser("remove", help="Remove a video from a collection")
    co.add_argument("name", help="Collection name")
    co.add_argument("targets", nargs="+", help="URLs or numeric video IDs to remove")
    co.set_defaults(func=cmd_collection)

    co = cosub.add_parser("delete", help="Delete a whole collection (members are unlinked, videos remain)")
    co.add_argument("name")
    co.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    co.set_defaults(func=cmd_collection)

    co = cosub.add_parser("add-by-creator", help="Add every analyzed post by a creator")
    co.add_argument("name", help="Collection name")
    co.add_argument("handle", help="Creator handle (with or without @)")
    co.set_defaults(func=cmd_collection)

    co = cosub.add_parser("add-by-topic", help="Add every post tagged with a topic")
    co.add_argument("name", help="Collection name")
    co.add_argument("topic", help="Topic label, case-insensitive substring")
    co.set_defaults(func=cmd_collection)

    co = cosub.add_parser("export", help="Export a collection to stdout or a file (markdown by default)")
    co.add_argument("name", help="Collection name")
    co.add_argument("--format", choices=["md", "json", "txt"], default="md")
    co.add_argument("--full", action="store_true",
                    help="Include full transcripts (default: compact, summary only)")
    co.add_argument("--max-words", type=int, default=0,
                    help="With --full, truncate each transcript to ~N words (0=no truncation)")
    co.add_argument("--out", "-o", help="Write to file instead of stdout")
    co.set_defaults(func=cmd_collection)

    sp = sub.add_parser("worker", help="Run the job queue worker")
    sp.add_argument("--once", action="store_true", help="Drain queue once and exit")
    sp.add_argument("--max-jobs", type=int, default=None, help="Stop after N jobs")
    sp.set_defaults(func=cmd_worker)

    sp = sub.add_parser("setup-r2", help="Test R2 connection")
    sp.set_defaults(func=cmd_setup_r2)

    sp = sub.add_parser("backup-db", help="Snapshot SQLite to local + R2")
    sp.set_defaults(func=cmd_backup_db)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", 0))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
