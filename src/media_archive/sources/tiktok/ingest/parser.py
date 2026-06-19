"""
Parse TikTok data export JSON/ZIP into the videos table.

TikTok ships exports in a few variants over the years. We use defensive
key-walking via _dig() so a missing nested field is None, not a crash.

Phase 1.6 addition: extract_creators_from_export() builds the seed list
for creators.yaml.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.exc import IntegrityError

from media_archive.core.db.schemas import (
    IngestRun,
    Video,
    get_session,
    init_db,
)
from media_archive.sources.tiktok.ingest.urls import (
    extract_handle,
    extract_video_id,
    normalize_tiktok_url,
)

logger = logging.getLogger(__name__)


def _dig(d: Any, *keys: str) -> Any:
    """Walk nested dict keys safely. Returns None on any miss/type-mismatch."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


def _parse_date(raw: Any) -> _dt.datetime | None:
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        try:
            return _dt.datetime.fromtimestamp(raw, tz=_dt.timezone.utc)
        except (OSError, ValueError):
            return None
    if not isinstance(raw, str):
        return None
    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ]
    for fmt in fmts:
        try:
            return _dt.datetime.strptime(raw, fmt).replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Collection extraction
# ---------------------------------------------------------------------------

def _collect_video_entries(data: Any) -> Iterable[tuple[str, dict]]:
    """Yield (collection_name, entry_dict) pairs from the parsed export.

    Handles multiple TikTok export shapes by checking known paths.
    """
    if not isinstance(data, dict):
        return

    # Known sections that contain video lists
    sections = {
        "Liked Videos": _dig(data, "Activity", "Like List", "ItemFavoriteList"),
        "Favorite Videos": _dig(data, "Activity", "Favorite Videos", "FavoriteVideoList"),
        "Watched Videos": _dig(data, "Activity", "Video Browsing History", "VideoList"),
        "Saved Videos": _dig(data, "Activity", "Favorite Videos", "FavoriteVideoList"),
        "Shared Videos": _dig(data, "Activity", "Share History", "ShareHistoryList"),
    }
    # Newer export format
    sections.setdefault("Liked Videos", _dig(data, "Activity", "Like List"))
    sections.setdefault("Browsing History", _dig(data, "Your Activity", "Watch History"))

    for name, entries in sections.items():
        if not entries:
            continue
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    yield name, entry
        elif isinstance(entries, dict):
            for entry in entries.values():
                if isinstance(entry, dict):
                    yield name, entry


def _entry_to_row(collection: str, entry: dict) -> dict | None:
    """Map a raw export entry to Video kwargs."""
    raw_url = entry.get("Link") or entry.get("VideoLink") or entry.get("url")
    if not raw_url:
        return None
    url = normalize_tiktok_url(raw_url)
    return {
        "url": url,
        "source": "export",
        "collection_name": collection,
        "platform": "tiktok",
        "interaction_date": _parse_date(entry.get("Date") or entry.get("date")),
        "video_id": extract_video_id(url),
        "author_handle": extract_handle(url),
        "description": entry.get("Description") or entry.get("description"),
    }


# ---------------------------------------------------------------------------
# Ingest entrypoint
# ---------------------------------------------------------------------------

def ingest_export(path: Path) -> dict:
    """Ingest a TikTok export ZIP or JSON file.

    Returns: {"added": N, "skipped": M, "errors": K}
    """
    init_db()
    if not path.exists():
        raise FileNotFoundError(path)

    raw_text = _read_export_text(path)
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Could not parse export as JSON: {e}") from e

    session = get_session()
    run = IngestRun(kind="export", source_path=str(path))
    session.add(run)
    session.commit()
    run_id = run.id

    added = 0
    skipped = 0
    errors = 0

    try:
        for collection, entry in _collect_video_entries(data):
            row = _entry_to_row(collection, entry)
            if not row:
                continue
            try:
                video = Video(**row)
                session.add(video)
                session.commit()
                added += 1
            except IntegrityError:
                session.rollback()
                skipped += 1
            except Exception as e:
                session.rollback()
                errors += 1
                logger.warning("Failed to insert export entry: %s", e)
    finally:
        run = session.get(IngestRun, run_id)
        if run is not None:
            run.finished_at = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
            run.rows_added = added
            run.rows_skipped = skipped
            session.commit()
        session.close()

    return {"added": added, "skipped": skipped, "errors": errors}


def _read_export_text(path: Path) -> str:
    """Return the JSON content of an export, handling both raw .json and .zip."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            json_names = [n for n in zf.namelist() if n.lower().endswith(".json")]
            if not json_names:
                raise ValueError(f"No JSON files found in {path}")
            # Prefer files that look like the master export
            json_names.sort(key=lambda n: ("user_data" not in n.lower(), len(n)))
            with zf.open(json_names[0]) as f:
                return f.read().decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Phase 1.6: extract creators
# ---------------------------------------------------------------------------

def extract_creators_from_export(path: Path) -> list[dict]:
    """Parse an export and return a deduped list of {handle, video_count, sample_url}.

    Used by `tiktok-archive creator import-from-export` to populate creators.yaml.
    """
    if not path.exists():
        raise FileNotFoundError(path)

    raw_text = _read_export_text(path)
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Could not parse export as JSON: {e}") from e

    counts: dict[str, dict] = {}
    for collection, entry in _collect_video_entries(data):
        row = _entry_to_row(collection, entry)
        if not row:
            continue
        handle = row.get("author_handle")
        if not handle:
            continue
        info = counts.setdefault(handle, {"handle": handle, "video_count": 0, "sample_url": row["url"]})
        info["video_count"] += 1

    # Sort by video count desc so the most-watched creators rank first
    return sorted(counts.values(), key=lambda x: x["video_count"], reverse=True)
