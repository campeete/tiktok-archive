"""
Creator-sync logic.

- add(): register a new creator from a handle
- import_from_export(): seed creators.yaml from a TikTok export
- sync(): pull new videos for one creator and enqueue analyze jobs
- sync_all(): same, for every enabled creator

Idempotency: each creator has last_seen_video_id. We stop walking the profile
once we hit it. First-run uses sync_depth (full / last-6mo / last-50).

Rate limit: list_creator_videos goes through downloader._wait_for_rate_limit,
so we share the same TikTok throttle as single-video downloads.
"""
from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Iterable

import yaml
from sqlalchemy.exc import IntegrityError

from media_archive.core import config
from media_archive.sources.tiktok.ingest.downloader import list_creator_videos
from media_archive.core.db.schemas import (
    Creator,
    Video,
    get_session,
    init_db,
)
from media_archive.sources.tiktok.ingest.urls import (
    creator_profile_url,
    extract_handle,
    extract_video_id,
    normalize_handle,
    normalize_tiktok_url,
)

logger = logging.getLogger(__name__)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Add / list creators
# ---------------------------------------------------------------------------

def add_creator(
    handle: str,
    *,
    display_name: str | None = None,
    sync_depth: str | None = None,
    notes: str | None = None,
    important: bool | None = None,
) -> Creator:
    """Register a new creator. Idempotent — re-adding bumps display_name/notes."""
    init_db()
    handle = normalize_handle(handle)
    if not handle:
        raise ValueError("handle is required")

    session = get_session()
    try:
        existing = (
            session.query(Creator).filter(Creator.handle == handle).one_or_none()
        )
        if existing:
            if display_name:
                existing.display_name = display_name
            if notes is not None:
                existing.notes = notes
            if important is not None:
                existing.important = bool(important)
            existing.enabled = True
            session.commit()
            session.refresh(existing)
            return existing

        creator = Creator(
            handle=handle,
            display_name=display_name or handle,
            profile_url=creator_profile_url(handle),
            sync_depth=sync_depth or config.CREATOR_DEFAULT_DEPTH,
            notes=notes,
            important=bool(important) if important is not None else False,
        )
        session.add(creator)
        try:
            session.commit()
            session.refresh(creator)
            return creator
        except IntegrityError:
            session.rollback()
            return session.query(Creator).filter(Creator.handle == handle).one()
    finally:
        session.close()


def list_creators() -> list[Creator]:
    """Return all creators, ordered by handle."""
    init_db()
    session = get_session()
    try:
        return list(session.query(Creator).order_by(Creator.handle.asc()).all())
    finally:
        session.close()


def disable_creator(handle: str) -> bool:
    init_db()
    handle = normalize_handle(handle)
    session = get_session()
    try:
        creator = session.query(Creator).filter(Creator.handle == handle).one_or_none()
        if not creator:
            return False
        creator.enabled = False
        session.commit()
        return True
    finally:
        session.close()


def remove_creator(handle: str) -> bool:
    """Hard-delete a creator. Their videos remain (creator_id goes NULL)."""
    init_db()
    handle = normalize_handle(handle)
    session = get_session()
    try:
        creator = session.query(Creator).filter(Creator.handle == handle).one_or_none()
        if not creator:
            return False
        # Null out FK on videos
        for v in session.query(Video).filter(Video.creator_id == creator.id).all():
            v.creator_id = None
        session.delete(creator)
        session.commit()
        return True
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Import from export / yaml
# ---------------------------------------------------------------------------

def import_from_export(export_path: Path, *, min_video_count: int = 1) -> dict:
    """Parse a TikTok export and add a Creator row for each unique handle.

    Returns counts: {added, updated, skipped}.
    """
    from media_archive.sources.tiktok.ingest.parser import extract_creators_from_export

    items = extract_creators_from_export(export_path)
    added = updated = skipped = 0
    for item in items:
        if item["video_count"] < min_video_count:
            skipped += 1
            continue
        try:
            existing = list_creators()
            handle = normalize_handle(item["handle"])
            was_existing = any(c.handle == handle for c in existing)
            add_creator(
                handle,
                display_name=item.get("display_name") or handle,
                notes=f"Auto-imported from export ({item['video_count']} videos)",
            )
            if was_existing:
                updated += 1
            else:
                added += 1
        except Exception as e:
            logger.warning("Failed to add creator %s: %s", item.get("handle"), e)
            skipped += 1
    return {"added": added, "updated": updated, "skipped": skipped, "total_seen": len(items)}


def import_from_yaml(yaml_path: Path | None = None) -> dict:
    """Load creators from creators.yaml.

    YAML format:
      creators:
        - handle: someuser
          display_name: Some User       # optional
          sync_depth: full              # optional
          notes: cybersecurity          # optional
        - handle: another
    """
    yaml_path = yaml_path or config.CREATORS_PATH
    if not yaml_path.is_file():
        return {"added": 0, "updated": 0, "errors": 0, "missing_file": True}

    with yaml_path.open() as f:
        data = yaml.safe_load(f) or {}
    entries = data.get("creators") or []
    if not isinstance(entries, list):
        raise ValueError(f"{yaml_path}: 'creators' must be a list")

    added = updated = errors = 0
    existing_handles = {c.handle for c in list_creators()}
    for entry in entries:
        if isinstance(entry, str):
            entry = {"handle": entry}
        if not isinstance(entry, dict):
            errors += 1
            continue
        try:
            handle = normalize_handle(entry.get("handle") or "")
            if not handle:
                errors += 1
                continue
            already = handle in existing_handles
            add_creator(
                handle,
                display_name=entry.get("display_name"),
                sync_depth=entry.get("sync_depth"),
                notes=entry.get("notes"),
                important=entry.get("important"),
            )
            if already:
                updated += 1
            else:
                added += 1
        except Exception as e:
            logger.warning("Failed to import %r: %s", entry, e)
            errors += 1
    return {"added": added, "updated": updated, "errors": errors}


def export_to_yaml(yaml_path: Path | None = None) -> Path:
    """Dump current creators table to a YAML file."""
    yaml_path = yaml_path or config.CREATORS_PATH
    creators = list_creators()
    data = {
        "creators": [
            {
                "handle": c.handle,
                "display_name": c.display_name,
                "sync_depth": c.sync_depth,
                "notes": c.notes,
                "enabled": c.enabled,
                "important": c.important,
            }
            for c in creators
        ]
    }
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    with yaml_path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
    return yaml_path


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _depth_to_max_videos(depth: str | None) -> int | None:
    if not depth or depth == "full":
        return None
    if depth == "last-50":
        return 50
    if depth == "last-6mo":
        # Approximation: assume ~3 posts/day max → ~540 in 6 months. Cap to be safe.
        return 200
    return 50


def sync_creator(handle: str) -> dict:
    """Pull new videos from one creator's profile and enqueue analyze jobs.

    Returns: {handle, new_videos, error?}
    """
    from media_archive.core.queue import enqueue

    init_db()
    handle = normalize_handle(handle)
    session = get_session()
    try:
        creator = session.query(Creator).filter(Creator.handle == handle).one_or_none()
        if creator is None:
            raise ValueError(f"Creator '{handle}' not registered. Run: tiktok-archive creator add {handle}")
        if not creator.enabled:
            return {"handle": handle, "new_videos": 0, "skipped": "disabled"}

        max_videos = _depth_to_max_videos(creator.sync_depth) if creator.last_synced_at is None else 50
    finally:
        session.close()

    entries, error = list_creator_videos(handle, max_videos=max_videos)
    if error:
        _mark_creator_error(handle, error)
        return {"handle": handle, "new_videos": 0, "error": error}

    # Walk entries; stop when we hit last_seen_video_id
    new_video_ids: list[int] = []
    init_db()
    session = get_session()
    try:
        creator = session.query(Creator).filter(Creator.handle == handle).one()
        last_seen = creator.last_seen_video_id
        first_seen_this_run: str | None = None

        for entry in entries:
            vid = str(entry.get("id") or "")
            if not vid:
                continue
            if first_seen_this_run is None:
                first_seen_this_run = vid

            if last_seen and vid == last_seen:
                # Caught up to the previous sync cursor
                break

            url = entry.get("url") or entry.get("webpage_url") or f"https://www.tiktok.com/@{handle}/video/{vid}"
            url = normalize_tiktok_url(url)

            # Insert a Video row in 'pending' state if not already present
            existing = (
                session.query(Video)
                .filter(
                    Video.url == url,
                    Video.source == "creator-sync",
                    Video.collection_name == handle,
                )
                .one_or_none()
            )
            if existing:
                continue

            video = Video(
                url=url,
                source="creator-sync",
                collection_name=handle,
                platform="tiktok",
                video_id=vid,
                author_handle=handle,
                creator_id=creator.id,
                description=entry.get("title"),
                duration_sec=entry.get("duration"),
                download_status="pending",
            )
            session.add(video)
            try:
                session.flush()
                new_video_ids.append(video.id)
            except IntegrityError:
                session.rollback()
                continue

        # Update sync cursor
        if first_seen_this_run is not None:
            creator.last_seen_video_id = first_seen_this_run
        creator.last_synced_at = _utcnow()
        creator.sync_error = None
        creator.sync_error_count = 0
        session.commit()

        # Enqueue analyze jobs (one per new video)
        for vid in new_video_ids:
            enqueue("download", video_id=vid, session=session)
        session.commit()

        return {"handle": handle, "new_videos": len(new_video_ids)}
    finally:
        session.close()


def sync_all(*, only_due: bool = True) -> list[dict]:
    """Sync every enabled creator. If only_due=True, skip creators synced recently."""
    init_db()
    session = get_session()
    try:
        q = session.query(Creator).filter(Creator.enabled.is_(True))
        creators = list(q.all())
    finally:
        session.close()

    cutoff = _utcnow() - _dt.timedelta(hours=config.CREATOR_SYNC_INTERVAL_HOURS)
    results = []
    for c in creators:
        if only_due and c.last_synced_at is not None and c.last_synced_at >= cutoff:
            continue
        try:
            results.append(sync_creator(c.handle))
        except Exception as e:
            logger.exception("Failed to sync %s", c.handle)
            results.append({"handle": c.handle, "new_videos": 0, "error": str(e)})
    return results


def _mark_creator_error(handle: str, error: str) -> None:
    init_db()
    session = get_session()
    try:
        creator = session.query(Creator).filter(Creator.handle == handle).one_or_none()
        if creator:
            creator.sync_error = error[:2000]
            creator.sync_error_count = (creator.sync_error_count or 0) + 1
            session.commit()
    finally:
        session.close()
