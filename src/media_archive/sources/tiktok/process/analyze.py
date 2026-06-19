"""
Single-video analyze orchestrator.

Used by:
- The CLI `analyze` command
- The web UI's POST /api/analyze
- The job queue worker (for creator-sync videos)

Pipeline:
1. Find or create Video row (natural key: url+source+collection)
2. Download to scratch dir
3. Extract metadata, populate row
4. Transcribe
5. **Delete the .mp4** (we don't keep videos)
6. Tag (summary, key_points, topics, intent, claim_check)
7. Write transcript JSON to storage backend
8. Mark video as transcript_only

This is idempotent: if step 4 has already happened for this URL, we skip
re-downloading. Likewise for tagging.
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
from media_archive.sources.tiktok.ingest.downloader import (
    DownloadResult,
    download_video,
)
from media_archive.core.db.schemas import Video, get_session, init_db
from media_archive.sources.tiktok.ingest.urls import (
    extract_handle,
    extract_video_id,
    is_photo_post,
    normalize_tiktok_url,
)
from media_archive.sources.tiktok.process import tag as tag_module
from media_archive.core.transcribe.transcribe import transcribe_video_file
from media_archive.core.storage import make_storage

logger = logging.getLogger(__name__)


def _is_creator_important(handle: str | None) -> bool:
    """Look up the `important` flag for a creator handle in the DB.

    Returns False on any error or if the creator isn't in the DB.
    """
    if not handle:
        return False
    init_db()
    session = get_session()
    try:
        from media_archive.core.db.schemas import Creator
        c = session.query(Creator).filter_by(handle=handle).one_or_none()
        return bool(c and c.important)
    except Exception:
        return False
    finally:
        session.close()


def _post_tag_pipeline(
    *,
    video_id: int,
    transcript: str | None,
    summary: str | None,
    author_handle: str | None,
    video_file: Path | None,
    keep_video: bool,
    is_photo_post_flag: bool,
    photo_image_urls: list[str] | None = None,
) -> None:
    """Run importance judgment + media extraction + discard.

    Called after tagging from each analyze entry point. Errors are logged
    but never raised — none of this is on the critical path; the
    transcript IS the canonical artifact and we already have it.

    For videos: extracts uniform thumbs always, scene-change full frames
    if important, then discards the .mp4 (unless keep_video=True).

    For photos: extracts slide thumbs always, full-res slides if important.
    No discard (photos never had a local .mp4).
    """
    # 1) Judge importance
    try:
        from media_archive.sources.tiktok.process.importance import judge_importance
        creator_imp = _is_creator_important(author_handle)
        judgment = judge_importance(
            transcript=transcript,
            summary=summary,
            creator_important=creator_imp,
        )
        logger.info(
            "Importance for video %d: %s (source=%s)",
            video_id, judgment.important, judgment.source,
        )
    except Exception as e:
        logger.warning("Importance judgment failed for video %d: %s", video_id, e)
        # Default to not-important; the cheap auto-rule fired separately
        # if applicable, which is what really matters.
        from media_archive.sources.tiktok.process.importance import ImportanceJudgment
        judgment = ImportanceJudgment(
            important=False, reason=f"judgment error: {e}", source="llm",
        )

    # Persist judgment
    try:
        init_db()
        session = get_session()
        try:
            video = session.get(Video, video_id)
            if video and not video.important_overridden:
                video.is_important = judgment.important
                video.importance_reason = judgment.reason
                if is_photo_post_flag:
                    video.post_type = "photo"
                session.commit()
        finally:
            session.close()
    except Exception as e:
        logger.warning("Could not persist importance for video %d: %s", video_id, e)

    # 2) Media extraction
    try:
        from media_archive.sources.tiktok.process import media as media_module
        if is_photo_post_flag:
            if photo_image_urls:
                result = media_module.extract_photo_slides(
                    video_id=video_id,
                    image_urls=photo_image_urls,
                    is_important=judgment.important,
                )
                logger.info(
                    "Photo slides for video %d: %d thumbs, %d full",
                    video_id, result.thumbs_created, result.full_frames_created,
                )
        elif video_file is not None and video_file.is_file():
            result = media_module.extract_video_frames(
                video_id=video_id,
                video_path=video_file,
                is_important=judgment.important,
            )
            logger.info(
                "Frames for video %d: %d thumbs, %d full",
                video_id, result.thumbs_created, result.full_frames_created,
            )
    except Exception as e:
        logger.warning("Media extraction failed for video %d: %s", video_id, e)

    # 3) Discard the .mp4 (videos only — photos never had one).
    # This is the transcribe-and-discard policy that was previously inline
    # right after transcription. Now deferred to here so media extraction
    # can use the file. Idempotent — if already deleted, nothing happens.
    if not is_photo_post_flag and video_file is not None and not keep_video:
        try:
            video_file.unlink(missing_ok=True)
            logger.info("Deleted %s after media extraction", video_file)
        except OSError as e:
            logger.warning("Failed to delete %s: %s", video_file, e)

        try:
            init_db()
            session = get_session()
            try:
                video = session.get(Video, video_id)
                if video:
                    video.transcript_only = True
                    video.file_path = None
                    session.commit()
            finally:
                session.close()
        except Exception as e:
            logger.warning("Could not flag transcript_only for video %d: %s", video_id, e)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_video(
    *,
    url: str,
    source: str = "analyzed",
    collection_name: str = "",
    creator_id: int | None = None,
) -> int:
    """Find a Video row matching the natural key, or insert one. Returns id."""
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
            # Reset any stuck 'downloading' or 'failed' state
            if existing.download_status in ("downloading", "failed"):
                existing.download_status = "pending"
                existing.download_error = None
                session.commit()
            return existing.id

        video = Video(
            url=url,
            source=source,
            collection_name=collection_name,
            platform="tiktok",
            video_id=extract_video_id(url),
            author_handle=extract_handle(url),
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
    """Update a Video row with metadata yt-dlp returned."""
    if not info:
        return
    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video is None:
            return
        video.video_id = video.video_id or str(info.get("id") or "")
        video.author_handle = video.author_handle or info.get("uploader") or info.get("channel")
        video.author_display_name = info.get("uploader") or info.get("channel")
        video.description = info.get("description") or info.get("title")
        video.duration_sec = float(info.get("duration") or 0) or None
        video.view_count = info.get("view_count")
        video.like_count = info.get("like_count")
        video.thumbnail_url = info.get("thumbnail")
        ts = info.get("timestamp") or info.get("upload_date_ts")
        if ts:
            try:
                video.upload_date = _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc)
            except (TypeError, ValueError):
                pass
        elif info.get("upload_date"):
            try:
                video.upload_date = _dt.datetime.strptime(
                    str(info["upload_date"]), "%Y%m%d"
                ).replace(tzinfo=_dt.timezone.utc)
            except ValueError:
                pass
        session.commit()
    finally:
        session.close()


def _write_transcript_artifact(video_id: int, payload: dict) -> None:
    """Write transcript JSON to storage backend.

    Key: transcripts/{video_id}.json
    """
    storage = make_storage()
    key = f"transcripts/{video_id}.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    storage.put(key, body)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_url(
    raw_url: str,
    *,
    source: str = "analyzed",
    collection_name: str = "",
    creator_id: int | None = None,
    keep_video: bool = False,
) -> dict:
    """Run the full pipeline on one URL. Returns a result dict.

    keep_video=True overrides the default transcribe-and-discard behavior.
    """
    url = normalize_tiktok_url(raw_url)
    started = time.time()
    logger.info("Analyzing %s", url)

    video_id = _get_or_create_video(
        url=url, source=source, collection_name=collection_name, creator_id=creator_id
    )

    # ---- Download ----
    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if not video:
            raise RuntimeError("Video row vanished")
        video.download_status = "downloading"
        video.download_attempts = (video.download_attempts or 0) + 1
        session.commit()
    finally:
        session.close()

    config.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    # ===== Branch on post type =====
    # Photo posts go through the photo processor (web HTML + audio/OCR).
    # Video posts go through yt-dlp + Whisper as normal.
    if is_photo_post(url):
        return _analyze_photo_post(
            url=url,
            video_id=video_id,
            started=started,
        )

    # ===== Video path (yt-dlp + Whisper) =====
    result: DownloadResult = download_video(
        url, config.SCRATCH_DIR, filename_template=f"{video_id}.%(ext)s"
    )
    if not result.success:
        _mark_download_failed(video_id, result.error or "download failed")
        return {
            "ok": False,
            "stage": "download",
            "error": result.error,
            "video_id": video_id,
            "rate_limited": result.rate_limited,
        }

    video_file = result.file_path
    assert video_file is not None
    _populate_metadata(video_id, result.info or {})

    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video:
            video.file_path = str(video_file)
            video.file_size_bytes = video_file.stat().st_size
            video.download_status = "downloaded"
            video.downloaded_at = _utcnow()
            session.commit()
    finally:
        session.close()

    # ---- Transcribe ----
    from media_archive.core.transcribe.transcribe import NoAudioStreamError
    try:
        text, lang = transcribe_video_file(video_file)
    except NoAudioStreamError as e:
        # Expected failure mode — known signature, no stack trace needed.
        # Most common cause: yt-dlp gave us back an HTML error page
        # because the URL was malformed (e.g. trailing punctuation from
        # a shell paste) or the post was deleted/region-locked.
        logger.warning("No audio stream in download for video %d: %s", video_id, e)
        _mark_download_failed(video_id, f"no audio stream: {e}")
        return {
            "ok": False,
            "stage": "transcribe",
            "error": str(e),
            "error_type": "no_audio_stream",
            "video_id": video_id,
        }
    except Exception as e:
        # Unexpected — keep the full stack trace.
        logger.exception("Transcription failed for video %d", video_id)
        _mark_download_failed(video_id, f"transcribe error: {e}")
        # Keep the video file for debugging on transcribe failure
        return {
            "ok": False,
            "stage": "transcribe",
            "error": str(e),
            "video_id": video_id,
        }

    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video:
            video.transcript = text
            video.transcript_lang = lang
            video.transcribed_at = _utcnow()
            session.commit()
    finally:
        session.close()

    # NOTE: discard of .mp4 deferred to the very end, after media frame
    # extraction has run. v1.7.0 needs the video file to extract uniform
    # thumbnails (always) and scene-change frames (when important).
    # Discard happens once we're done with frames.

    # ---- Tag (or stub for empty transcripts) ----
    # If the transcript is empty or very short, the post likely has no
    # transcribable speech (music-only photo carousel, silent video, etc).
    # Don't waste an Ollama call on garbage; write a stub instead.
    MIN_TRANSCRIPT_CHARS = 20
    is_empty = len((text or "").strip()) < MIN_TRANSCRIPT_CHARS
    is_photo = is_photo_post(url)

    if is_empty:
        if is_photo:
            stub_summary = "Photo post with no transcribable audio (music-only or silent slideshow)."
        else:
            stub_summary = "Video has no transcribable speech (music-only or silent)."
        tag_result = {
            "summary": stub_summary,
            "key_points": [],
            "topics": [],
            "intent": "other",
            "claim_check": False,
            "_stub": True,
            "_post_type": "photo" if is_photo else "video",
        }
        logger.info(
            "Video %d: empty transcript (%d chars), skipping tag pass and writing stub",
            video_id, len((text or "").strip()),
        )
    else:
        try:
            tag_result = tag_module.tag_transcript(text)
        except Exception as e:
            logger.exception("Tagging failed for video %d", video_id)
            return {
                "ok": False,
                "stage": "tag",
                "error": str(e),
                "video_id": video_id,
            }

    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video:
            video.summary = tag_result.get("summary")
            video.key_points = json.dumps(tag_result.get("key_points") or [])
            video.topics = json.dumps(tag_result.get("topics") or [])
            video.intent = tag_result.get("intent")
            video.claim_check = bool(tag_result.get("claim_check"))
            video.tag_summary = json.dumps(tag_result, ensure_ascii=False)
            video.tagged_at = _utcnow()
            session.commit()
    finally:
        session.close()

    # ---- Importance + media extraction + discard (Phase 1.7) ----
    _post_tag_pipeline(
        video_id=video_id,
        transcript=text,
        summary=tag_result.get('summary'),
        author_handle=(result.info or {}).get('uploader'),
        video_file=video_file,
        keep_video=keep_video,
        is_photo_post_flag=False,
    )

    # ---- Persist transcript JSON to storage ----
    artifact = {
        "video_id": video_id,
        "url": url,
        "transcript": text,
        "transcript_lang": lang,
        "summary": tag_result.get("summary"),
        "key_points": tag_result.get("key_points"),
        "topics": tag_result.get("topics"),
        "intent": tag_result.get("intent"),
        "claim_check": tag_result.get("claim_check"),
        "metadata": {
            "author_handle": (result.info or {}).get("uploader"),
            "duration_sec": (result.info or {}).get("duration"),
            "upload_date": (result.info or {}).get("upload_date"),
            "view_count": (result.info or {}).get("view_count"),
            "like_count": (result.info or {}).get("like_count"),
        },
        "analyzed_at": _utcnow().isoformat(),
    }
    try:
        _write_transcript_artifact(video_id, artifact)
        init_db()
        session = get_session()
        try:
            video = session.get(Video, video_id)
            if video and config.STORAGE_BACKEND == "r2":
                video.r2_synced_at = _utcnow()
                session.commit()
        finally:
            session.close()
    except Exception as e:
        logger.warning("Failed to write transcript artifact: %s", e)

    elapsed = time.time() - started
    logger.info("Analyzed video %d in %.1fs", video_id, elapsed)

    return {
        "ok": True,
        "video_id": video_id,
        "url": url,
        "transcript": text,
        "transcript_lang": lang,
        "summary": tag_result.get("summary"),
        "key_points": tag_result.get("key_points"),
        "topics": tag_result.get("topics"),
        "intent": tag_result.get("intent"),
        "claim_check": tag_result.get("claim_check"),
        "elapsed_sec": elapsed,
    }


def analyze_local_file(path: Path, **kwargs: Any) -> dict:
    """Analyze a local video file (no download). For uploads."""
    if not path.exists():
        raise FileNotFoundError(path)

    started = time.time()
    fake_url = f"file://{path.resolve()}"

    video_id = _get_or_create_video(
        url=fake_url,
        source="upload",
        collection_name="",
    )

    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video:
            video.file_path = str(path)
            video.file_size_bytes = path.stat().st_size
            video.download_status = "downloaded"
            video.downloaded_at = _utcnow()
            session.commit()
    finally:
        session.close()

    try:
        text, lang = transcribe_video_file(path)
    except Exception as e:
        return {"ok": False, "stage": "transcribe", "error": str(e), "video_id": video_id}

    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video:
            video.transcript = text
            video.transcript_lang = lang
            video.transcribed_at = _utcnow()
            session.commit()
    finally:
        session.close()

    # Same near-empty transcript handling as analyze_url
    is_empty = len((text or "").strip()) < 20
    if is_empty:
        tag_result = {
            "summary": "Uploaded file has no transcribable speech (silent or music-only).",
            "key_points": [],
            "topics": [],
            "intent": "other",
            "claim_check": False,
            "_stub": True,
        }
    else:
        try:
            tag_result = tag_module.tag_transcript(text)
        except Exception as e:
            return {"ok": False, "stage": "tag", "error": str(e), "video_id": video_id}

    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video:
            video.summary = tag_result.get("summary")
            video.key_points = json.dumps(tag_result.get("key_points") or [])
            video.topics = json.dumps(tag_result.get("topics") or [])
            video.intent = tag_result.get("intent")
            video.claim_check = bool(tag_result.get("claim_check"))
            video.tag_summary = json.dumps(tag_result, ensure_ascii=False)
            video.tagged_at = _utcnow()
            session.commit()
    finally:
        session.close()

    # ---- Importance + media extraction (Phase 1.7) ----
    # local-file analyze: the file at  is the source. We don't
    # discard it — the user provided it, it's their file, not ours.
    _post_tag_pipeline(
        video_id=video_id,
        transcript=text,
        summary=tag_result.get('summary'),
        author_handle=None,
        video_file=path,
        keep_video=True,  # never delete user-provided files
        is_photo_post_flag=False,
    )

    return {
        "ok": True,
        "video_id": video_id,
        "url": fake_url,
        "transcript": text,
        "transcript_lang": lang,
        "summary": tag_result.get("summary"),
        "key_points": tag_result.get("key_points"),
        "topics": tag_result.get("topics"),
        "intent": tag_result.get("intent"),
        "claim_check": tag_result.get("claim_check"),
        "elapsed_sec": time.time() - started,
    }


def _analyze_photo_post(*, url: str, video_id: int, started: float) -> dict:
    """Run the analyze pipeline for a TikTok photo post.

    Mirrors analyze_url's video flow: fetches the post, transcribes any audio
    (and optionally OCRs slide text), tags the result with the LLM, and
    persists the artifact through the same storage backend.

    Photo posts have no video file to delete, so the keep_video flag is N/A.
    """
    from media_archive.sources.tiktok.process import photo as photo_module

    pr = photo_module.fetch_and_process(url)

    if not pr.success:
        _mark_download_failed(video_id, pr.error or "photo fetch failed")
        return {
            "ok": False,
            "stage": "download",
            "error": pr.error,
            "video_id": video_id,
            "post_type": "photo",
        }

    # Update the row with what we learned (handle, description, image count)
    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video:
            video.author_handle = video.author_handle or pr.author_handle
            video.author_display_name = pr.author_display_name
            video.description = pr.description
            video.download_status = "downloaded"
            video.downloaded_at = _utcnow()
            video.transcript = pr.transcript
            video.transcript_lang = pr.transcript_lang
            video.transcribed_at = _utcnow()
            # Photo posts have no .mp4 — flag transcript_only immediately
            video.transcript_only = True
            video.file_path = None
            if pr.upload_timestamp:
                try:
                    video.upload_date = _dt.datetime.fromtimestamp(
                        int(pr.upload_timestamp), tz=_dt.timezone.utc
                    ).replace(tzinfo=None)
                except (TypeError, ValueError):
                    pass
            session.commit()
    finally:
        session.close()

    # ---- Tag (or stub for empty transcripts) ----
    text = pr.transcript or ""
    is_empty = len(text.strip()) < 20
    if is_empty:
        if pr.image_count and not pr.audio_url:
            stub_summary = (
                f"Photo post with {pr.image_count} slide(s) and no audio "
                "(silent slideshow). No transcribable content."
            )
        elif pr.image_count:
            stub_summary = (
                f"Photo post with {pr.image_count} slide(s). Audio was music-only "
                "or too brief to transcribe. Install tesseract for slide OCR."
            )
        else:
            stub_summary = "Photo post with no extractable content."
        tag_result = {
            "summary": stub_summary,
            "key_points": [],
            "topics": [],
            "intent": "other",
            "claim_check": False,
            "_stub": True,
            "_post_type": "photo",
            "_image_count": pr.image_count,
            "_transcript_source": pr.transcript_source,
        }
        logger.info(
            "Photo post %d: empty transcript (%d chars), writing stub",
            video_id, len(text.strip()),
        )
    else:
        try:
            tag_result = tag_module.tag_transcript(text)
            # Annotate that this came from a photo post for analytics
            tag_result["_post_type"] = "photo"
            tag_result["_transcript_source"] = pr.transcript_source
            tag_result["_image_count"] = pr.image_count
        except Exception as e:
            logger.exception("Tagging failed for photo post %d", video_id)
            return {
                "ok": False,
                "stage": "tag",
                "error": str(e),
                "video_id": video_id,
                "post_type": "photo",
            }

    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video:
            video.summary = tag_result.get("summary")
            video.key_points = json.dumps(tag_result.get("key_points") or [])
            video.topics = json.dumps(tag_result.get("topics") or [])
            video.intent = tag_result.get("intent")
            video.claim_check = bool(tag_result.get("claim_check"))
            video.tag_summary = json.dumps(tag_result, ensure_ascii=False)
            video.tagged_at = _utcnow()
            session.commit()
    finally:
        session.close()

    # ---- Importance + media extraction (Phase 1.7) ----
    _post_tag_pipeline(
        video_id=video_id,
        transcript=text,
        summary=tag_result.get('summary'),
        author_handle=pr.author_handle,
        video_file=None,
        keep_video=False,
        is_photo_post_flag=True,
        photo_image_urls=pr.image_urls,
    )

    # ---- Persist transcript JSON to storage ----
    artifact = {
        "video_id": video_id,
        "url": url,
        "post_type": "photo",
        "transcript": text,
        "transcript_lang": pr.transcript_lang,
        "transcript_source": pr.transcript_source,
        "summary": tag_result.get("summary"),
        "key_points": tag_result.get("key_points"),
        "topics": tag_result.get("topics"),
        "intent": tag_result.get("intent"),
        "claim_check": tag_result.get("claim_check"),
        "metadata": {
            "author_handle": pr.author_handle,
            "author_display_name": pr.author_display_name,
            "description": pr.description,
            "image_count": pr.image_count,
            "image_urls": pr.image_urls,
            "audio_url": pr.audio_url,
            "upload_timestamp": pr.upload_timestamp,
        },
        "analyzed_at": _utcnow().isoformat(),
    }
    try:
        _write_transcript_artifact(video_id, artifact)
        init_db()
        session = get_session()
        try:
            video = session.get(Video, video_id)
            if video and config.STORAGE_BACKEND == "r2":
                video.r2_synced_at = _utcnow()
                session.commit()
        finally:
            session.close()
    except Exception as e:
        logger.warning("Failed to write photo transcript artifact: %s", e)

    elapsed = time.time() - started
    logger.info("Analyzed photo post %d in %.1fs", video_id, elapsed)

    return {
        "ok": True,
        "video_id": video_id,
        "url": url,
        "post_type": "photo",
        "transcript": text,
        "transcript_lang": pr.transcript_lang,
        "transcript_source": pr.transcript_source,
        "image_count": pr.image_count,
        "summary": tag_result.get("summary"),
        "key_points": tag_result.get("key_points"),
        "topics": tag_result.get("topics"),
        "intent": tag_result.get("intent"),
        "claim_check": tag_result.get("claim_check"),
        "elapsed_sec": elapsed,
    }


def _mark_download_failed(video_id: int, error: str) -> None:
    init_db()
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video:
            video.download_status = "failed"
            video.download_error = error[:2000] if error else None
            session.commit()
    finally:
        session.close()


def cleanup_stuck_rows() -> int:
    """Reset any rows stuck in 'downloading' or 'failed' so they can be re-tried.

    Useful after a crash; run automatically on worker startup.
    """
    init_db()
    session = get_session()
    try:
        rows = (
            session.query(Video)
            .filter(Video.download_status.in_(["downloading", "failed"]))
            .all()
        )
        n = 0
        for r in rows:
            r.download_status = "pending"
            r.download_error = None
            n += 1
        session.commit()
        return n
    finally:
        session.close()
