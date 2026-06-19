"""Tests for media_archive.core.queue — atomic claim, backoff, recovery."""
import datetime as _dt
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="tt-test-q-"))
    monkeypatch.setenv("TT_DATA_DIR", str(tmp))
    monkeypatch.setenv("TT_DB_PATH", str(tmp / "test.db"))
    monkeypatch.setenv("TT_DB_URL", f"sqlite:///{tmp / 'test.db'}")
    monkeypatch.setenv("TT_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("TT_JOB_BACKOFF_BASE_SEC", "1")  # speed up test backoffs
    monkeypatch.setenv("TT_JOB_BACKOFF_FACTOR", "2")
    monkeypatch.setenv("TT_JOB_MAX_ATTEMPTS", "3")
    import importlib
    import media_archive.core.config as cfg
    importlib.reload(cfg)
    import media_archive.core.db.schemas as schemas
    schemas._engine = None
    schemas._SessionLocal = None
    # v0.3.0: do not reload schemas, just dispose the engine so init_db re-reads DB_URL
    if schemas._engine is not None:
        schemas._engine.dispose()
    schemas._engine = None
    schemas._SessionLocal = None
    schemas.DB_URL = cfg.DB_URL
    import media_archive.core.queue as q
    importlib.reload(q)
    yield tmp


def test_enqueue_and_claim():
    from media_archive.core import queue
    from media_archive.core.db.schemas import init_db
    init_db()
    job = queue.enqueue("download", payload={"url": "x"})
    assert job.id is not None

    claimed = queue.claim_next_job()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == "running"
    assert claimed.attempts == 1


def test_claim_returns_none_when_empty():
    from media_archive.core import queue
    from media_archive.core.db.schemas import init_db
    init_db()
    assert queue.claim_next_job() is None


def test_complete_job_marks_done():
    from media_archive.core import queue
    from media_archive.core.db.schemas import Job, get_session, init_db
    init_db()
    job = queue.enqueue("download")
    claimed = queue.claim_next_job()
    queue.complete_job(claimed.id)

    session = get_session()
    try:
        fresh = session.get(Job, job.id)
        assert fresh.status == "done"
        assert fresh.finished_at is not None
        assert fresh.locked_at is None
    finally:
        session.close()


def test_fail_with_retry_reschedules():
    from media_archive.core import queue
    from media_archive.core.db.schemas import Job, get_session, init_db
    init_db()
    queue.enqueue("download")
    claimed = queue.claim_next_job()
    queue.fail_job(claimed.id, "test error", retry=True)

    session = get_session()
    try:
        fresh = session.get(Job, claimed.id)
        assert fresh.status == "pending"
        assert fresh.last_error == "test error"
        # Scheduled for the future
        now = _dt.datetime.now(_dt.timezone.utc)
        # SQLite returns naive datetimes; fresh.scheduled_for is naive
        scheduled = fresh.scheduled_for
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=_dt.timezone.utc)
        assert scheduled > now - _dt.timedelta(seconds=1)
    finally:
        session.close()


def test_fail_after_max_attempts_marks_failed():
    from media_archive.core import queue
    from media_archive.core.db.schemas import Job, get_session, init_db
    init_db()
    queue.enqueue("download")

    # Fail 3 times (matches TT_JOB_MAX_ATTEMPTS=3)
    for _ in range(3):
        claimed = queue.claim_next_job()
        if claimed is None:
            # Stuck behind backoff in tests; force-bump scheduled_for
            session = get_session()
            try:
                jobs = session.query(Job).filter(Job.status == "pending").all()
                for j in jobs:
                    j.scheduled_for = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=10)
                session.commit()
            finally:
                session.close()
            claimed = queue.claim_next_job()
        assert claimed is not None
        queue.fail_job(claimed.id, "boom", retry=True)

    session = get_session()
    try:
        # Find the original job
        job = session.query(Job).first()
        assert job.status == "failed"
        assert job.attempts == 3
    finally:
        session.close()


def test_no_retry_marks_failed_immediately():
    from media_archive.core import queue
    from media_archive.core.db.schemas import Job, get_session, init_db
    init_db()
    queue.enqueue("download")
    claimed = queue.claim_next_job()
    queue.fail_job(claimed.id, "fatal", retry=False)
    session = get_session()
    try:
        fresh = session.get(Job, claimed.id)
        assert fresh.status == "failed"
    finally:
        session.close()


def test_reset_stuck_jobs():
    from media_archive.core import queue
    from media_archive.core.db.schemas import Job, get_session, init_db
    init_db()
    queue.enqueue("download")
    claimed = queue.claim_next_job()
    assert claimed.status == "running"

    # Manually backdate locked_at to simulate a stuck job
    session = get_session()
    try:
        job = session.get(Job, claimed.id)
        job.locked_at = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)
        session.commit()
    finally:
        session.close()

    n = queue.reset_stuck_jobs()
    assert n == 1

    session = get_session()
    try:
        job = session.get(Job, claimed.id)
        assert job.status == "pending"
        assert job.locked_at is None
    finally:
        session.close()


def test_queue_stats():
    from media_archive.core import queue
    from media_archive.core.db.schemas import init_db
    init_db()
    queue.enqueue("download")
    queue.enqueue("download")
    queue.enqueue("tag")
    stats = queue.queue_stats()
    assert stats["by_status"].get("pending") == 3
    assert "download" in stats["by_kind"]
    assert "tag" in stats["by_kind"]


def test_concurrent_claim_atomic():
    """Two claim_next_job calls on the same row return None for one of them."""
    from media_archive.core import queue
    from media_archive.core.db.schemas import init_db
    init_db()
    queue.enqueue("download")
    a = queue.claim_next_job()
    b = queue.claim_next_job()
    assert a is not None
    assert b is None  # nothing left
