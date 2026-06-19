"""
SQLAlchemy schemas for tiktok-archive.

Tables:
- creators:      handles being followed for periodic sync
- videos:        analyzed clips (one row per unique URL+source)
- jobs:          work queue for the worker process
- tags:          controlled vocabulary (Phase 4)
- video_tags:    many-to-many between videos and tags
- ingest_runs:   audit log of bulk ingest operations

The job queue is the heart of Phase 1.6 — it lets us drain hundreds of videos
asynchronously without blocking the CLI or web UI.
"""
from __future__ import annotations

import datetime as _dt
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from media_archive.core.config import DB_URL, ensure_dirs


def _utcnow() -> _dt.datetime:
    """Naive UTC. SQLite has no tz; we keep everything naive UTC for
    consistency between writes and reads."""
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Creators
# ---------------------------------------------------------------------------

class Creator(Base):
    """A TikTok creator we're following.

    Use `handle` (without @) as the natural key. We allow re-adding by handle
    so the row survives a profile_url rename.
    """

    __tablename__ = "creators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    handle: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    profile_url: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_depth: Mapped[str] = mapped_column(
        String(20), default="last-6mo", nullable=False
    )  # full | last-6mo | last-50
    last_synced_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    last_seen_video_id: Mapped[str | None] = mapped_column(String(100))
    sync_error: Mapped[str | None] = mapped_column(Text)
    sync_error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    # Phase 1.7: creators marked important get scene-change frame extraction
    # for ALL their videos, regardless of per-post LLM judgment.
    important: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    videos: Mapped[list["Video"]] = relationship(back_populates="creator")


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

class Video(Base):
    """A single piece of media (video or photo post) that has been or will be analyzed.

    Natural key: (url, source, collection_name).

    `platform`  — content origin: 'tiktok', 'youtube', 'instagram', 'local', etc.
                  Added in media-archive v0.1.0 (Phase 2). Defaults to 'tiktok'
                  for backward-compat with the v1.7.x SQLite DB.
    `source`    — ingestion path: 'analyzed' (single-shot), 'creator-sync', 'export', 'bulk'.
                  Pre-existing column from v1.x; describes how the row entered the DB,
                  not where the content lives.
    `collection_name` — export collection (e.g. 'Liked Videos') or creator handle for
                  creator-sync, or "" for single-shot analyze.

    NOTE: collection_name is NOT NULL with default "" because SQLite treats
    NULL as distinct, which would break dedup on the natural key.
    """

    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Natural key
    url: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="analyzed")
    collection_name: Mapped[str] = mapped_column(
        String(200), nullable=False, default=""
    )

    # Content platform — added in v0.1.0. Indexed because the multi-source filtering
    # (e.g. "show me only TikTok posts" or "rebuild YouTube embeddings") will hit
    # this column on every read path.
    platform: Mapped[str] = mapped_column(
        String(30), nullable=False, default="tiktok", index=True
    )

    # Source metadata
    interaction_date: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    video_id: Mapped[str | None] = mapped_column(String(100), index=True)
    author_handle: Mapped[str | None] = mapped_column(String(100), index=True)
    author_display_name: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    upload_date: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    view_count: Mapped[int | None] = mapped_column(Integer)
    like_count: Mapped[int | None] = mapped_column(Integer)
    thumbnail_url: Mapped[str | None] = mapped_column(String(500))

    # Relationships
    creator_id: Mapped[int | None] = mapped_column(
        ForeignKey("creators.id"), index=True
    )
    creator: Mapped[Creator | None] = relationship(back_populates="videos")

    # Download tracking
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    download_status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )  # pending | downloading | downloaded | failed | skipped
    download_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    download_error: Mapped[str | None] = mapped_column(Text)
    downloaded_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)

    # Once true, we've discarded the .mp4 to save disk; transcript is the
    # canonical artifact. Set after a successful transcribe-and-discard cycle.
    transcript_only: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Transcription
    transcript: Mapped[str | None] = mapped_column(Text)
    transcript_lang: Mapped[str | None] = mapped_column(String(10))
    transcribed_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)

    # Tagging / analysis
    summary: Mapped[str | None] = mapped_column(Text)
    key_points: Mapped[str | None] = mapped_column(Text)  # JSON array
    topics: Mapped[str | None] = mapped_column(Text)  # JSON array
    intent: Mapped[str | None] = mapped_column(String(50))
    claim_check: Mapped[bool | None] = mapped_column(Boolean)
    tag_summary: Mapped[str | None] = mapped_column(Text)  # JSON
    tagged_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)

    # Embedding
    embedded_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)

    # R2 sync
    r2_synced_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)

    # Phase 1.7: post type and importance.
    # post_type: 'video' (default) or 'photo'. Set during analyze.
    # is_important: set by the LLM judge after tagging, OR by user override
    #   in the web UI, OR by the empty-transcript auto-rule (slides ARE the
    #   message). When true, frame extraction produces full-res frames.
    # importance_reason: free-text from the LLM, kept for UI display.
    # important_overridden: tracks manual override so we don't re-judge.
    post_type: Mapped[str] = mapped_column(
        String(20), default="video", nullable=False, index=True
    )
    is_important: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    importance_reason: Mapped[str | None] = mapped_column(Text)
    important_overridden: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "url", "source", "collection_name", name="uix_video_natural"
        ),
        Index("ix_videos_status_creator", "download_status", "creator_id"),
        Index("ix_videos_created", "created_at"),
    )


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------

class Job(Base):
    """A single piece of pipeline work for the worker.

    Status flow: pending -> running -> done | failed
    On failure, a job that hasn't exhausted attempts will go pending again
    after `scheduled_for` (exponential backoff).
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    kind: Mapped[str] = mapped_column(
        String(30), nullable=False, index=True
    )  # download | transcribe | tag | embed | sync-creator
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id"), index=True
    )
    creator_id: Mapped[int | None] = mapped_column(
        ForeignKey("creators.id"), index=True
    )

    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False, index=True
    )  # pending | running | done | failed
    payload: Mapped[str | None] = mapped_column(Text)  # JSON, kind-specific args

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    locked_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    locked_by: Mapped[str | None] = mapped_column(String(100))

    scheduled_for: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False, index=True
    )
    started_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    duration_sec: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_jobs_pending", "status", "kind", "scheduled_for"),
    )


# ---------------------------------------------------------------------------
# Tag vocabulary
# ---------------------------------------------------------------------------

class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(50), index=True)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


class VideoTag(Base):
    __tablename__ = "video_tags"

    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id"), primary_key=True
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Ingest run audit log
# ---------------------------------------------------------------------------

class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    finished_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    rows_added: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Collections (v0.3.0)
# ---------------------------------------------------------------------------

class Collection(Base):
    """A user-defined named grouping of videos.

    Collections are the unit of "send a chunk of my archive to Claude."
    They're hand-curated (or built from filters like creator/tag/date) and
    serialized into a single markdown export that can be pasted into a chat.

    The natural key is `name` — names are unique, case-preserved, and used
    on the CLI as the addressable handle (e.g. `media-archive collection
    show offsec-notes`). Two collections can't share a name.
    """
    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    members: Mapped[list["CollectionMember"]] = relationship(
        back_populates="collection",
        order_by="CollectionMember.position",
        cascade="all, delete-orphan",
    )


class CollectionMember(Base):
    """Join row connecting a Collection to a Video, with explicit ordering.

    `position` lets the user re-order without changing membership. We
    store positions as integers but don't compact them on remove — gaps
    are fine, the UI just reads in ascending order. New entries get
    position = (current max + 1) so appends are O(1).

    Adding the same (collection, video) twice is a UniqueConstraint
    violation, which the CLI handles gracefully (skip with a notice).
    """
    __tablename__ = "collection_members"
    __table_args__ = (
        UniqueConstraint("collection_id", "video_id", name="uix_collection_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)  # optional per-member annotation

    collection: Mapped["Collection"] = relationship(back_populates="members")
    video: Mapped["Video"] = relationship()


# ---------------------------------------------------------------------------
# Media artifacts (Phase 1.7)
# ---------------------------------------------------------------------------

class MediaArtifact(Base):
    """An image saved alongside a video or photo post.

    Kinds:
    - frame_thumb: 256px-wide JPEG, uniform-sampled video frame
    - frame_full:  full-res JPEG at a scene-change point (only for important)
    - slide_thumb: 256px-wide JPEG, photo-post slide thumbnail
    - slide_full:  full-res photo-post slide (only for important / empty xcript)

    sequence is the in-order index (0-based). For uniform frames, sequence
    matches sample order. For scene-change frames, sequence matches scene
    detection order. For slides, sequence matches the original imagePost
    array order.

    timestamp_sec is set for video frames (the position in the source clip).
    Null for photo slides.
    """

    __tablename__ = "media_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp_sec: Mapped[float | None] = mapped_column(Float)

    # Storage
    local_path: Mapped[str | None] = mapped_column(String(500))
    r2_key: Mapped[str | None] = mapped_column(String(500))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("video_id", "kind", "sequence", name="uix_media_natural"),
        Index("ix_media_video_kind", "video_id", "kind"),
    )


# ---------------------------------------------------------------------------
# Engine + session factory
# ---------------------------------------------------------------------------

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _):  # pragma: no cover
    """Enable WAL mode and tune SQLite for our workload."""
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    finally:
        cursor.close()


def _migrate_schema(engine: Engine) -> None:
    """Idempotent migrations for v1.7+ schema changes.

    SQLAlchemy's create_all() makes new tables but won't add columns to
    existing ones. We use SQLite's ALTER TABLE ADD COLUMN, which is
    safe-to-rerun via PRAGMA introspection.

    We do NOT use a full migration framework like Alembic because the
    schema churn is low and Cameron's deployments are single-user/single-
    machine. If the project grows past that, replace this with Alembic.
    """
    from sqlalchemy import inspect, text
    if not DB_URL.startswith("sqlite"):
        return

    insp = inspect(engine)
    if "videos" not in insp.get_table_names():
        return  # fresh DB; create_all() handled everything

    with engine.begin() as conn:
        existing_video_cols = {c["name"] for c in insp.get_columns("videos")}
        for col_name, col_def in [
            ("post_type", "VARCHAR(20) NOT NULL DEFAULT 'video'"),
            ("is_important", "BOOLEAN NOT NULL DEFAULT 0"),
            ("importance_reason", "TEXT"),
            ("important_overridden", "BOOLEAN NOT NULL DEFAULT 0"),
            # v0.1.0 (Phase 2): content platform. Defaults to 'tiktok' so that
            # rows from the v1.7.x DB get correctly classified at migrate time.
            ("platform", "VARCHAR(30) NOT NULL DEFAULT 'tiktok'"),
        ]:
            if col_name not in existing_video_cols:
                conn.execute(text(f"ALTER TABLE videos ADD COLUMN {col_name} {col_def}"))

        # Index on platform (idempotent — IF NOT EXISTS only works in SQLite ≥ 3.8)
        existing_indexes = {idx["name"] for idx in insp.get_indexes("videos")}
        if "ix_videos_platform" not in existing_indexes:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_platform ON videos(platform)"))

        existing_creator_cols = {c["name"] for c in insp.get_columns("creators")}
        if "important" not in existing_creator_cols:
            conn.execute(
                text("ALTER TABLE creators ADD COLUMN important BOOLEAN NOT NULL DEFAULT 0")
            )


def init_db() -> Engine:
    """Initialize the engine + create tables. Idempotent."""
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    ensure_dirs()
    _engine = create_engine(
        DB_URL,
        future=True,
        echo=False,
        connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
    )
    Base.metadata.create_all(_engine)
    _migrate_schema(_engine)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def get_session() -> Session:
    """Return a new Session. Caller is responsible for closing/committing."""
    if _SessionLocal is None:
        init_db()
    assert _SessionLocal is not None
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Convenience context manager: commit on success, rollback on error."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
