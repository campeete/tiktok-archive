"""
Job queue: SQLite-backed work queue for the worker process.

Atomicity model: claim_next_job() uses a single UPDATE with a WHERE that
includes status='pending', so two workers cannot grab the same job. Locks
include a `locked_by` UUID and `locked_at` timestamp so we can detect and
reset stuck jobs (worker crash mid-job).

Each job kind:
- download:    given a video_id, run yt-dlp to fetch the .mp4 to scratch
- transcribe:  run Whisper on the scratch .mp4, save transcript, delete .mp4
- tag:         run summary/key-points/intent via Ollama, write JSON
- embed:       compute embeddings (Phase 5)
- sync-creator: pull new video URLs from a creator's profile, enqueue downloads

Backoff is exponential: 30s, 90s, 270s on failures. After JOB_MAX_ATTEMPTS, the
job lands in 'failed' permanently and we surface it in stats.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import socket
import uuid
from typing import Any

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from media_archive.core import config
from media_archive.core.db.schemas import Job, get_session, init_db

logger = logging.getLogger(__name__)


def _utcnow() -> _dt.datetime:
    """Naive UTC datetime, matching what SQLite stores.

    SQLite has no tz support; SQLAlchemy returns naive datetimes from
    DateTime columns. Mixing tz-aware and naive raises TypeError on
    subtraction, so we keep everything naive UTC inside the DB layer.
    """
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _worker_identity() -> str:
    """A stable-ish identity for this worker process for lock attribution."""
    return f"{socket.gethostname()}/{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

def enqueue(
    kind: str,
    *,
    video_id: int | None = None,
    creator_id: int | None = None,
    payload: dict[str, Any] | None = None,
    delay_sec: float = 0.0,
    session: Session | None = None,
) -> Job:
    """Add a single job to the queue.

    If `session` is provided, the caller owns the transaction. Otherwise we
    open a fresh session, commit, then return a detached-but-readable copy.
    """
    own_session = session is None
    if own_session:
        init_db()
        session = get_session()
    assert session is not None

    scheduled = _utcnow() + _dt.timedelta(seconds=delay_sec)
    job = Job(
        kind=kind,
        video_id=video_id,
        creator_id=creator_id,
        status="pending",
        payload=json.dumps(payload) if payload else None,
        scheduled_for=scheduled,
    )
    session.add(job)
    if own_session:
        try:
            session.commit()
            # Capture column values BEFORE closing the session, so the caller
            # can read .id without triggering a refresh on a detached instance.
            session.refresh(job)
            session.expunge(job)
        finally:
            session.close()
    return job


def enqueue_many(jobs: list[dict[str, Any]]) -> int:
    """Bulk enqueue. Each dict has keys: kind, video_id, creator_id, payload."""
    init_db()
    session = get_session()
    try:
        count = 0
        for spec in jobs:
            enqueue(
                kind=spec["kind"],
                video_id=spec.get("video_id"),
                creator_id=spec.get("creator_id"),
                payload=spec.get("payload"),
                session=session,
            )
            count += 1
        session.commit()
        return count
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

def claim_next_job(
    *,
    kinds: list[str] | None = None,
    worker_id: str | None = None,
    session: Session | None = None,
) -> Job | None:
    """Atomically claim the next eligible pending job, or return None.

    Eligibility: status='pending', scheduled_for <= now, and (optionally)
    kind is in `kinds`.

    Strategy: SELECT one candidate, then UPDATE WHERE id=... AND status='pending'
    to claim. If the UPDATE matches 0 rows, another worker beat us — try again.
    Bounded retries.
    """
    own_session = session is None
    if own_session:
        init_db()
        session = get_session()
    assert session is not None

    worker_id = worker_id or _worker_identity()
    now = _utcnow()

    try:
        for _ in range(5):  # try up to 5 races
            q = session.query(Job).filter(
                Job.status == "pending",
                Job.scheduled_for <= now,
            )
            if kinds:
                q = q.filter(Job.kind.in_(kinds))
            candidate = q.order_by(Job.scheduled_for.asc(), Job.id.asc()).first()
            if candidate is None:
                return None

            # Atomic claim
            stmt = (
                update(Job)
                .where(and_(Job.id == candidate.id, Job.status == "pending"))
                .values(
                    status="running",
                    locked_at=now,
                    locked_by=worker_id,
                    started_at=now,
                    attempts=Job.attempts + 1,
                )
            )
            result = session.execute(stmt)
            session.commit()
            if result.rowcount == 1:
                # Re-fetch with fresh state, then detach so the caller can
                # read attributes after we close the session.
                session.refresh(candidate)
                if own_session:
                    session.expunge(candidate)
                return candidate
            # Lost the race; loop to next candidate
        return None
    finally:
        if own_session:
            session.close()


# ---------------------------------------------------------------------------
# Complete / fail
# ---------------------------------------------------------------------------

def complete_job(job_id: int) -> None:
    """Mark a job as done. Idempotent."""
    init_db()
    session = get_session()
    try:
        now = _utcnow()
        job = session.get(Job, job_id)
        if job is None:
            return
        job.status = "done"
        job.finished_at = now
        if job.started_at:
            delta = (now - job.started_at).total_seconds()
            job.duration_sec = delta
        job.locked_at = None
        job.locked_by = None
        session.commit()
    finally:
        session.close()


def fail_job(job_id: int, error: str, *, retry: bool = True) -> None:
    """Mark a job as failed. If retry=True and attempts < max, re-queue with backoff."""
    init_db()
    session = get_session()
    try:
        now = _utcnow()
        job = session.get(Job, job_id)
        if job is None:
            return

        # Truncate error message so we don't blow up the column
        if error and len(error) > 4000:
            error = error[:4000] + "...[truncated]"
        job.last_error = error

        if retry and job.attempts < config.JOB_MAX_ATTEMPTS:
            backoff = config.JOB_BACKOFF_BASE_SEC * (
                config.JOB_BACKOFF_FACTOR ** (job.attempts - 1)
            )
            job.status = "pending"
            job.scheduled_for = now + _dt.timedelta(seconds=backoff)
            job.locked_at = None
            job.locked_by = None
            logger.info(
                "Job %d (kind=%s) failed, retrying in %.0fs (attempt %d/%d)",
                job.id, job.kind, backoff, job.attempts, config.JOB_MAX_ATTEMPTS,
            )
        else:
            job.status = "failed"
            job.finished_at = now
            if job.started_at:
                delta = (now - job.started_at).total_seconds()
                job.duration_sec = delta
            job.locked_at = None
            job.locked_by = None
            logger.warning(
                "Job %d (kind=%s) permanently failed after %d attempts: %s",
                job.id, job.kind, job.attempts, error[:200] if error else "(no msg)",
            )
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Stuck-job recovery
# ---------------------------------------------------------------------------

def reset_stuck_jobs() -> int:
    """Reset any 'running' jobs whose lock has timed out. Called on worker startup."""
    init_db()
    session = get_session()
    try:
        cutoff = _utcnow() - _dt.timedelta(seconds=config.JOB_LOCK_TIMEOUT_SEC)
        stmt = (
            update(Job)
            .where(
                and_(
                    Job.status == "running",
                    or_(Job.locked_at.is_(None), Job.locked_at < cutoff),
                )
            )
            .values(
                status="pending",
                locked_at=None,
                locked_by=None,
            )
        )
        result = session.execute(stmt)
        session.commit()
        n = result.rowcount or 0
        if n:
            logger.warning("Reset %d stuck jobs", n)
        return n
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def queue_stats() -> dict:
    """Return aggregate counts for stats display."""
    init_db()
    session = get_session()
    try:
        from sqlalchemy import func

        rows = (
            session.query(Job.kind, Job.status, func.count(Job.id))
            .group_by(Job.kind, Job.status)
            .all()
        )
        # Shape: {kind: {status: count}}
        result: dict[str, dict[str, int]] = {}
        for kind, status, count in rows:
            result.setdefault(kind, {})[status] = count
        # Totals
        total_by_status: dict[str, int] = {}
        for kind_stats in result.values():
            for status, count in kind_stats.items():
                total_by_status[status] = total_by_status.get(status, 0) + count
        # Recent failures (last 24h)
        cutoff = _utcnow() - _dt.timedelta(hours=24)
        recent_failures = (
            session.query(func.count(Job.id))
            .filter(Job.status == "failed", Job.finished_at >= cutoff)
            .scalar()
        ) or 0
        # Oldest pending
        oldest_pending = (
            session.query(func.min(Job.created_at))
            .filter(Job.status == "pending")
            .scalar()
        )
        return {
            "by_kind": result,
            "by_status": total_by_status,
            "recent_failures_24h": recent_failures,
            "oldest_pending": oldest_pending.isoformat() if oldest_pending else None,
        }
    finally:
        session.close()
