"""
Collection operations: create, add, remove, list, show, bulk-add.

Collections are user-defined named groupings of analyzed videos. They are
the unit Cameron uses to bundle related content (e.g. all OffSec course
notes, all Chicago hackerspace observations, all redtales stories) into a
single context blob that can be pasted into a Claude conversation.

All functions here take or return plain Python types (ints, strings,
dicts) rather than SQLAlchemy ORM objects, so the CLI and webapp can
both call them safely without leaking session state.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from media_archive.core.db.schemas import (
    Collection,
    CollectionMember,
    Video,
    get_session,
    init_db,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class CollectionError(Exception):
    """Base for all collection-layer errors."""


class CollectionNotFoundError(CollectionError):
    """Raised when a name lookup misses."""


class CollectionAlreadyExistsError(CollectionError):
    """Raised when create() hits a unique-name collision."""


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _resolve_video(session, identifier: str | int) -> Video | None:
    """Find a Video by either numeric id or URL.

    The CLI accepts either form (e.g. `add my-coll 42` or
    `add my-coll https://...`) and we resolve here so callers don't
    have to branch.
    """
    # Numeric ID path
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        return session.get(Video, int(identifier))
    # URL path — try exact match first, then with trailing-slash strip
    candidate = identifier.strip()
    return (
        session.query(Video).filter(Video.url == candidate).first()
        or session.query(Video).filter(Video.url == candidate.rstrip("/")).first()
    )


def _get_collection_by_name(session, name: str) -> Collection:
    """Look up a Collection by name or raise CollectionNotFoundError."""
    coll = session.query(Collection).filter(Collection.name == name).one_or_none()
    if coll is None:
        raise CollectionNotFoundError(
            f"No collection named {name!r}. "
            f"Use `media-archive collection list` to see existing collections."
        )
    return coll


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_collection(name: str, description: str | None = None) -> dict:
    """Create a new empty collection. Raises CollectionAlreadyExistsError
    if the name is taken.

    Names are validated lightly: must be non-empty and not contain
    whitespace at the boundaries (which would silently break CLI args).
    Internal whitespace is allowed but discouraged via convention.
    """
    name = (name or "").strip()
    if not name:
        raise CollectionError("Collection name cannot be empty.")
    if len(name) > 100:
        raise CollectionError("Collection name must be 100 characters or fewer.")

    init_db()
    session = get_session()
    try:
        coll = Collection(name=name, description=description)
        session.add(coll)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise CollectionAlreadyExistsError(
                f"A collection named {name!r} already exists. "
                f"Use a different name or `collection remove` it first."
            )
        return {
            "id": coll.id,
            "name": coll.name,
            "description": coll.description,
            "member_count": 0,
        }
    finally:
        session.close()


def delete_collection(name: str) -> int:
    """Delete a collection (and its membership rows via cascade).
    Returns the number of members that were removed."""
    init_db()
    session = get_session()
    try:
        coll = _get_collection_by_name(session, name)
        member_count = len(coll.members)
        session.delete(coll)
        session.commit()
        return member_count
    finally:
        session.close()


def add_video_to_collection(
    collection_name: str,
    video_identifier: str | int,
    *,
    note: str | None = None,
) -> dict:
    """Add one video (by id or URL) to the named collection.

    Returns {"added": bool, "reason": str|None, "video_id": int|None}.
    `added=False, reason="already_member"` means the video was already
    in the collection — a no-op, not an error.
    """
    init_db()
    session = get_session()
    try:
        coll = _get_collection_by_name(session, collection_name)
        video = _resolve_video(session, video_identifier)
        if video is None:
            return {
                "added": False,
                "reason": "video_not_found",
                "video_id": None,
                "detail": f"No video matches {video_identifier!r}.",
            }

        # Idempotent add: skip silently if already a member.
        existing = (
            session.query(CollectionMember)
            .filter(
                CollectionMember.collection_id == coll.id,
                CollectionMember.video_id == video.id,
            )
            .first()
        )
        if existing is not None:
            return {
                "added": False,
                "reason": "already_member",
                "video_id": video.id,
            }

        # Compute next position. We don't compact on removes, so this is
        # max(position)+1 even after deletes have left gaps — that's fine,
        # since order is what matters, not contiguity.
        max_pos = (
            session.query(func.max(CollectionMember.position))
            .filter(CollectionMember.collection_id == coll.id)
            .scalar()
        )
        next_pos = (max_pos or 0) + 1

        member = CollectionMember(
            collection_id=coll.id,
            video_id=video.id,
            position=next_pos,
            note=note,
        )
        session.add(member)
        session.commit()
        return {
            "added": True,
            "reason": None,
            "video_id": video.id,
            "position": next_pos,
        }
    finally:
        session.close()


def remove_video_from_collection(
    collection_name: str, video_identifier: str | int
) -> dict:
    """Remove a video from a collection. Returns {"removed": bool, ...}.

    Removing a non-member is a no-op (returns removed=False) rather
    than an error — collections are user-curated and idempotent
    behavior is what they want.
    """
    init_db()
    session = get_session()
    try:
        coll = _get_collection_by_name(session, collection_name)
        video = _resolve_video(session, video_identifier)
        if video is None:
            return {
                "removed": False,
                "reason": "video_not_found",
            }
        member = (
            session.query(CollectionMember)
            .filter(
                CollectionMember.collection_id == coll.id,
                CollectionMember.video_id == video.id,
            )
            .one_or_none()
        )
        if member is None:
            return {"removed": False, "reason": "not_member", "video_id": video.id}
        session.delete(member)
        session.commit()
        return {"removed": True, "video_id": video.id}
    finally:
        session.close()


def list_collections() -> list[dict]:
    """Return all collections with member counts, ordered by name."""
    init_db()
    session = get_session()
    try:
        # Single query with a member-count subquery to avoid N+1.
        rows = (
            session.query(
                Collection,
                func.count(CollectionMember.id).label("member_count"),
            )
            .outerjoin(CollectionMember, Collection.id == CollectionMember.collection_id)
            .group_by(Collection.id)
            .order_by(Collection.name)
            .all()
        )
        return [
            {
                "id": coll.id,
                "name": coll.name,
                "description": coll.description,
                "member_count": int(count),
                "created_at": coll.created_at,
                "updated_at": coll.updated_at,
            }
            for coll, count in rows
        ]
    finally:
        session.close()


def show_collection(name: str) -> dict:
    """Return a collection's metadata + ordered members with summaries.

    The members list is the same shape used by the export module — a
    short dict per video with summary, key_points, topics, transcript,
    etc. Heavy fields like full transcript text are included; the
    export step decides what to truncate.
    """
    init_db()
    session = get_session()
    try:
        coll = _get_collection_by_name(session, name)
        # Load members in position order with their videos eager-loaded
        # via the relationship. We hit one query for members + one for
        # the video objects via SQLAlchemy's identity map.
        members_data = []
        for member in coll.members:
            video = member.video
            if video is None:
                continue  # orphaned membership row; skip defensively
            members_data.append(_video_to_dict(video, member))
        return {
            "id": coll.id,
            "name": coll.name,
            "description": coll.description,
            "created_at": coll.created_at,
            "updated_at": coll.updated_at,
            "member_count": len(members_data),
            "members": members_data,
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Bulk-add helpers (build a collection from a query, not by hand)
# ---------------------------------------------------------------------------

def add_by_creator(collection_name: str, handle: str) -> dict:
    """Add every analyzed video by a given creator handle to the collection.

    Handle is matched case-insensitive against `videos.author_handle`
    (which is the @handle minus the @ for TikTok, channel ID for YouTube).
    """
    init_db()
    session = get_session()
    try:
        coll = _get_collection_by_name(session, collection_name)
        # Strip leading @ for forgiving CLI input
        handle = (handle or "").strip().lstrip("@").strip()
        if not handle:
            return {"added": 0, "skipped": 0, "error": "empty handle"}

        videos = (
            session.query(Video)
            .filter(func.lower(Video.author_handle) == handle.lower())
            .all()
        )
        added = 0
        skipped = 0
        for v in videos:
            existing = (
                session.query(CollectionMember)
                .filter(
                    CollectionMember.collection_id == coll.id,
                    CollectionMember.video_id == v.id,
                )
                .first()
            )
            if existing is not None:
                skipped += 1
                continue
            max_pos = (
                session.query(func.max(CollectionMember.position))
                .filter(CollectionMember.collection_id == coll.id)
                .scalar()
            ) or 0
            session.add(CollectionMember(
                collection_id=coll.id,
                video_id=v.id,
                position=max_pos + 1,
            ))
            session.flush()
            added += 1
        session.commit()
        return {"added": added, "skipped": skipped, "matched": len(videos)}
    finally:
        session.close()


def add_by_topic(collection_name: str, topic: str) -> dict:
    """Add every video whose `topics` field contains the given topic.

    Topics are stored as a JSON-encoded list in TEXT, so we use SQLite's
    JSON LIKE matching as a coarse filter and don't decode every row.
    Case-insensitive substring match — good enough for the bulk-add use
    case where the user is adding from memory ("everything tagged
    'cybersecurity'").
    """
    init_db()
    session = get_session()
    try:
        coll = _get_collection_by_name(session, collection_name)
        topic = (topic or "").strip()
        if not topic:
            return {"added": 0, "skipped": 0, "error": "empty topic"}

        # Coarse SQL filter: rows whose topics blob contains the substring.
        # We do final exact-match decoding in Python for correctness.
        candidates = (
            session.query(Video)
            .filter(Video.topics.isnot(None))
            .filter(Video.topics.like(f"%{topic}%"))
            .all()
        )

        import json
        added = 0
        skipped = 0
        for v in candidates:
            try:
                topics = json.loads(v.topics) if v.topics else []
            except (json.JSONDecodeError, TypeError):
                continue
            if not any(topic.lower() in (t or "").lower() for t in topics):
                continue
            existing = (
                session.query(CollectionMember)
                .filter(
                    CollectionMember.collection_id == coll.id,
                    CollectionMember.video_id == v.id,
                )
                .first()
            )
            if existing is not None:
                skipped += 1
                continue
            max_pos = (
                session.query(func.max(CollectionMember.position))
                .filter(CollectionMember.collection_id == coll.id)
                .scalar()
            ) or 0
            session.add(CollectionMember(
                collection_id=coll.id,
                video_id=v.id,
                position=max_pos + 1,
            ))
            session.flush()
            added += 1
        session.commit()
        return {"added": added, "skipped": skipped}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Internal: serialize a Video row + its membership context to a dict
# ---------------------------------------------------------------------------

def _video_to_dict(video: Video, member: CollectionMember | None = None) -> dict:
    """Convert a Video ORM row to a plain dict suitable for export.

    Includes everything the markdown formatter might want (transcript,
    summary, tags, etc.) — the formatter decides what to truncate or
    drop based on its mode.
    """
    import json
    # Decode JSON-stored list columns; tolerate stale bad data with []
    def _decode_list(blob: Any) -> list:
        if not blob:
            return []
        if isinstance(blob, list):
            return blob
        try:
            decoded = json.loads(blob)
            return decoded if isinstance(decoded, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    return {
        "id": video.id,
        "url": video.url,
        "platform": getattr(video, "platform", None) or "tiktok",
        "post_type": getattr(video, "post_type", None) or "video",
        "author_handle": video.author_handle,
        "author_display_name": getattr(video, "author_display_name", None),
        "title": getattr(video, "title", None) or getattr(video, "description", None),
        "duration_sec": video.duration_sec,
        "upload_date": video.upload_date,
        "summary": video.summary,
        "key_points": _decode_list(video.key_points),
        "topics": _decode_list(video.topics),
        "intent": video.intent,
        "claim_check": bool(video.claim_check) if video.claim_check is not None else None,
        "transcript": video.transcript,
        "transcript_lang": video.transcript_lang,
        "is_important": bool(getattr(video, "is_important", False)),
        "tagged_at": video.tagged_at,
        "position": member.position if member else None,
        "note": member.note if member else None,
    }
