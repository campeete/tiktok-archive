"""
YouTube analyze pipeline (v0.2.0).

Mirrors the TikTok analyze_url contract — same input, same JSON output —
but routes through:
  - YouTube URL parsing (sources/youtube/ingest/urls.py)
  - The shared yt-dlp downloader (sources/tiktok/ingest/downloader.py;
    yt-dlp is platform-agnostic, so the TikTok module's wrapper handles
    YouTube URLs identically)
  - Chunked Whisper transcription (core/transcribe/chunked.py) instead of
    the single-pass v1.x transcribe — this is the long-form story.
  - The same tag/embed pipeline as TikTok.

Where this duplicates code from `sources/tiktok/process/analyze.py`,
that's deliberate for v0.2.0: extracting the shared engine into core
will only be safe once we have a second source actually exercising the
contract. The boundary becomes obvious from real usage, not by guessing
upfront.

What's NOT in v0.2.0:
  - Playlist expansion (just-a-list URLs)
  - Channel sync (creator-sync analog)
  - YouTube comment ingestion
  - Live stream handling
These all land in v0.2.x point releases.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError

from media_archive.core import config
from media_archive.core.db.schemas import Video, get_session, init_db
from media_archive.core.transcribe.chunked import (
    ChunkedTranscript,
    chunked_transcribe_video_file,
)
from media_archive.core.transcribe.transcribe import NoAudioStreamError
from media_archive.sources.tiktok.ingest.downloader import (
    DownloadResult,
    download_video,
)
from media_archive.sources.tiktok.process import tag as tag_module
from media_archive.sources.youtube.ingest.urls import (
    extract_video_id,
    is_youtube_url,
    normalize_youtube_url,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (mirror the TikTok analyze.py shape)
# ---------------------------------------------------------------------------

def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _get_or_create_video(
    *,
    url: str,
    source: str = "analyzed",
    collection_name: str = "",
    creator_id: int | None = None,
) -> int:
    """Find a Video row matching (url, source, collection_name), or insert one.

    Sets platform='youtube' on insert. Existing rows are returned as-is
    regardless of their platform value — re-analyzing a URL that was
    previously inserted with the wrong platform is allowed; the caller
    can fix the platform value separately.
    """
    init_db()
    session = get_session()
    try:
        existing = (
            session.query(Video)
            .filter(
                Video.url == url,
                Video.source == source,
                Video.collection_name == collection_name,
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.download_status in ("downloading", "failed"):
                existing.download_status = "pending"
                existing.download_error = None
                session.commit()
            return existing.id

        video = Video(
            url=url,
            source=source,
            collection_name=collection_name,
            platform="youtube",
            video_id=extract_video_id(url),
            # author_handle on YouTube is the channel handle; we don't have
            # it from the URL alone (need yt-dlp metadata). Populated later
            # by _populate_metadata.
            author_handle=None,
            creator_id=creator_id,
            download_status="pending",
        )
        session.add(video)
        try:
            session.commit()
            return video.id
        except IntegrityError:
            session.rollback()
            existing = (
                session.query(Video)
                .filter(
                    Video.url == url,
                    Video.source == source,
                    Video.collection_name == collection_name,
                )
                .one()
            )
            return existing.id
    finally:
        session.close()


def _populate_metadata(video_id: int, info: dict) -> None:
    """Copy yt-dlp metadata onto the Video row.

    yt-dlp's info-dict keys are similar across platforms but not identical.
    For YouTube we get: title, description, uploader, channel, channel_id,
    duration, upload_date, view_count, like_count, thumbnail.
    """
    if not info:
        return
    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if not video:
            return

        # YouTube uses 'channel' / 'uploader' for the creator name and
        # 'uploader_id' / 'channel_id' for the stable channel ID.
        author_handle = info.get("uploader_id") or info.get("channel_id")
        author_display = info.get("channel") or info.get("uploader")

        video.author_handle = author_handle
        video.author_display_name = author_display
        video.description = info.get("description") or info.get("title")
        video.duration_sec = info.get("duration")
        video.view_count = info.get("view_count")
        video.like_count = info.get("like_count")
        video.thumbnail_url = info.get("thumbnail")

        # upload_date is yt-dlp's YYYYMMDD string
        upload_date = info.get("upload_date")
        if upload_date and isinstance(upload_date, str) and len(upload_date) == 8:
            try:
                video.upload_date = _dt.datetime.strptime(upload_date, "%Y%m%d")
            except ValueError:
                pass

        session.commit()
    finally:
        session.close()


def _mark_download_failed(video_id: int, error: str) -> None:
    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video:
            video.download_status = "failed"
            video.download_error = error[:500]
            session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_url(
    raw_url: str,
    *,
    source: str = "analyzed",
    collection_name: str = "",
    creator_id: int | None = None,
    keep_video: bool = False,
) -> dict:
    """Run the full YouTube analyze pipeline. Returns the same dict shape
    as the TikTok analyze_url for caller compatibility.

    Stages: validate → download → transcribe (chunked) → tag → discard.

    keep_video=True keeps the .mp4 around after transcribe (useful for
    debugging long-form failures where you want to re-run the chunker
    without re-downloading 500MB).
    """
    started = time.time()

    # Stage: validate
    if not is_youtube_url(raw_url):
        return {
            "ok": False,
            "stage": "validate",
            "error": (
                f"Not a YouTube URL: {raw_url}. The youtube source plugin "
                "only handles youtube.com and youtu.be URLs."
            ),
        }

    url = normalize_youtube_url(raw_url)
    vid_id = extract_video_id(url)
    if vid_id is None:
        return {
            "ok": False,
            "stage": "validate",
            "error": (
                f"YouTube URL has no recognizable video ID: {url}. "
                "Playlist-only and channel URLs are not yet supported "
                "(coming in v0.2.x)."
            ),
        }

    logger.info("Analyzing %s (youtube, id=%s)", url, vid_id)
    db_video_id = _get_or_create_video(
        url=url, source=source, collection_name=collection_name, creator_id=creator_id
    )

    # Stage: download
    init_db()
    session = get_session()
    try:
        video = session.get(Video, db_video_id)
        if not video:
            raise RuntimeError("Video row vanished")
        video.download_status = "downloading"
        video.download_attempts = (video.download_attempts or 0) + 1
        session.commit()
    finally:
        session.close()

    config.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    result: DownloadResult = download_video(
        url, config.SCRATCH_DIR, filename_template=f"{db_video_id}.%(ext)s"
    )
    if not result.success:
        _mark_download_failed(db_video_id, result.error or "download failed")
        return {
            "ok": False,
            "stage": "download",
            "error": result.error,
            "video_id": db_video_id,
            "rate_limited": result.rate_limited,
        }

    video_file = result.file_path
    assert video_file is not None
    _populate_metadata(db_video_id, result.info or {})

    init_db()
    session = get_session()
    try:
        video = session.get(Video, db_video_id)
        if video:
            video.file_path = str(video_file)
            video.file_size_bytes = video_file.stat().st_size
            video.download_status = "downloaded"
            video.downloaded_at = _utcnow()
            session.commit()
    finally:
        session.close()

    # Stage: transcribe (CHUNKED — this is the v0.2.0 win)
    try:
        transcript: ChunkedTranscript = chunked_transcribe_video_file(video_file)
    except NoAudioStreamError as e:
        logger.warning(
            "No audio stream in download for video %d: %s", db_video_id, e
        )
        _mark_download_failed(db_video_id, f"no audio stream: {e}")
        if not keep_video:
            try:
                video_file.unlink()
            except OSError:
                pass
        return {
            "ok": False,
            "stage": "transcribe",
            "error": str(e),
            "error_type": "no_audio_stream",
            "video_id": db_video_id,
        }
    except Exception as e:
        logger.exception("Transcription failed for video %d", db_video_id)
        _mark_download_failed(db_video_id, f"transcribe error: {e}")
        return {
            "ok": False,
            "stage": "transcribe",
            "error": str(e),
            "video_id": db_video_id,
        }

    init_db()
    session = get_session()
    try:
        video = session.get(Video, db_video_id)
        if video:
            video.transcript = transcript.text
            video.transcript_lang = transcript.language
            video.transcribed_at = _utcnow()
            # If yt-dlp didn't report duration, fall back to ffprobe's
            # measurement from the chunked transcribe.
            if not video.duration_sec and transcript.duration_sec:
                video.duration_sec = transcript.duration_sec
            session.commit()
    finally:
        session.close()

    # Stage: tag
    MIN_TRANSCRIPT_CHARS = 20
    is_empty = len((transcript.text or "").strip()) < MIN_TRANSCRIPT_CHARS
    if is_empty:
        tag_result = {
            "summary": "Video has no transcribable speech (music-only or silent).",
            "key_points": [],
            "topics": [],
            "intent": "other",
            "claim_check": False,
            "_stub": True,
        }
        logger.info(
            "Video %d: empty transcript (%d chars), skipping tag pass",
            db_video_id, len((transcript.text or "").strip()),
        )
    else:
        try:
            tag_result = tag_module.tag_transcript(transcript.text)
        except Exception as e:
            logger.exception("Tagging failed for video %d", db_video_id)
            return {
                "ok": False,
                "stage": "tag",
                "error": str(e),
                "video_id": db_video_id,
            }

    init_db()
    session = get_session()
    try:
        video = session.get(Video, db_video_id)
        if video:
            video.summary = tag_result.get("summary")
            video.key_points = json.dumps(tag_result.get("key_points") or [])
            video.topics = json.dumps(tag_result.get("topics") or [])
            video.intent = tag_result.get("intent")
            video.claim_check = bool(tag_result.get("claim_check"))
            video.tagged_at = _utcnow()
            session.commit()
    finally:
        session.close()

    # Stage: discard (transcribe-and-discard)
    if not keep_video:
        try:
            video_file.unlink()
            logger.info(
                "Deleted %s after transcribe (transcribe-and-discard)",
                video_file,
            )
        except OSError as e:
            logger.warning("Could not delete %s: %s", video_file, e)

    elapsed = time.time() - started
    logger.info(
        "Analyzed video %d in %.1fs (chunks=%d, segments=%d)",
        db_video_id, elapsed, transcript.chunk_count, len(transcript.segments),
    )

    return {
        "ok": True,
        "video_id": db_video_id,
        "url": url,
        "platform": "youtube",
        "transcript": transcript.text,
        "transcript_lang": transcript.language,
        "duration_sec": transcript.duration_sec,
        "chunk_count": transcript.chunk_count,
        "segment_count": len(transcript.segments),
        "summary": tag_result.get("summary"),
        "key_points": tag_result.get("key_points") or [],
        "topics": tag_result.get("topics") or [],
        "intent": tag_result.get("intent"),
        "claim_check": tag_result.get("claim_check", False),
        "elapsed_sec": elapsed,
    }
