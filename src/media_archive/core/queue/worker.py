"""
Worker process: drains the job queue.

Threading model: one ThreadPoolExecutor per kind, sized from config. Worker
continuously polls for pending jobs and dispatches them by kind.

Run forever:
  tiktok-archive worker

Drain once and exit (for cron):
  tiktok-archive worker --once

The worker is safe to restart at any time. Stuck jobs are reset on startup.
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Callable

from media_archive.core import config
from media_archive.core.db.schemas import Job, Video, get_session, init_db
from media_archive.sources.tiktok.process.analyze import analyze_url

# Re-export queue helpers under `queue` for clarity
from media_archive.core import queue as job_queue
from media_archive.sources.tiktok.sync import sync_creator

logger = logging.getLogger(__name__)


_shutdown = threading.Event()


def _handle_signal(signum, frame):  # pragma: no cover
    logger.info("Caught signal %d, shutting down gracefully...", signum)
    _shutdown.set()


# ---------------------------------------------------------------------------
# Job dispatchers
# ---------------------------------------------------------------------------

def _dispatch(job: Job) -> None:
    """Run one job to completion. Idempotent at the job-kind level."""
    kind = job.kind
    try:
        if kind == "download":
            _run_download_job(job)
        elif kind == "transcribe":
            # 'download' kind already does transcribe + tag inline because
            # analyze_url() is the cheapest way to keep the pipeline atomic.
            # If we ever split them, this is the hook.
            _run_download_job(job)
        elif kind == "tag":
            _run_download_job(job)  # same reason
        elif kind == "sync-creator":
            _run_sync_creator_job(job)
        else:
            raise ValueError(f"Unknown job kind: {kind}")
        job_queue.complete_job(job.id)
    except RateLimited as e:
        # Special-case: don't burn an attempt; reschedule far in the future
        job_queue.fail_job(job.id, f"rate limited: {e}", retry=True)
    except Exception as e:
        logger.exception("Job %d (kind=%s) failed", job.id, kind)
        job_queue.fail_job(job.id, str(e), retry=True)


class RateLimited(Exception):
    pass


def _run_download_job(job: Job) -> None:
    """Run analyze_url for the video this job points at."""
    if job.video_id is None:
        raise ValueError("download job missing video_id")
    init_db()
    session = get_session()
    try:
        video = session.get(Video, job.video_id)
        if video is None:
            raise ValueError(f"Video {job.video_id} not found")
        url = video.url
        source = video.source
        collection = video.collection_name
        creator_id = video.creator_id
    finally:
        session.close()

    result = analyze_url(
        url,
        source=source,
        collection_name=collection,
        creator_id=creator_id,
        keep_video=False,
    )
    if not result.get("ok"):
        if result.get("rate_limited"):
            raise RateLimited(result.get("error") or "tiktok rate limit")
        raise RuntimeError(f"{result.get('stage')}: {result.get('error')}")


def _run_sync_creator_job(job: Job) -> None:
    if job.creator_id is None:
        raise ValueError("sync-creator job missing creator_id")
    from media_archive.core.db.schemas import Creator
    init_db()
    session = get_session()
    try:
        creator = session.get(Creator, job.creator_id)
        if creator is None:
            raise ValueError(f"Creator {job.creator_id} not found")
        handle = creator.handle
    finally:
        session.close()

    result = sync_creator(handle)
    if "error" in result:
        raise RuntimeError(result["error"])


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

class Worker:
    def __init__(self) -> None:
        # Single dispatch executor with capacity covering all kinds. Per-kind
        # rate limiting is enforced inside each dispatcher (downloader does
        # its own throttle; Whisper is single-threaded so concurrent claims
        # of multiple transcribe jobs would just queue inside the threadpool).
        self.max_workers = (
            config.WORKER_DOWNLOAD_CONCURRENCY
            + config.WORKER_TRANSCRIBE_CONCURRENCY
            + config.WORKER_TAG_CONCURRENCY
            + config.WORKER_EMBED_CONCURRENCY
        )
        self.executor = ThreadPoolExecutor(
            max_workers=max(2, self.max_workers), thread_name_prefix="tt-worker"
        )
        self.in_flight: set[Future] = set()
        self.processed = 0

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

    def run_once(self, *, max_jobs: int | None = None) -> int:
        """Drain all pending jobs once, then return. Returns count processed."""
        job_queue.reset_stuck_jobs()
        processed = 0
        idle_strikes = 0
        while not _shutdown.is_set():
            if max_jobs is not None and processed >= max_jobs:
                break
            job = job_queue.claim_next_job()
            if job is None:
                if not self.in_flight:
                    break  # truly idle, exit
                idle_strikes += 1
                if idle_strikes > 3:
                    break
                self._reap()
                time.sleep(0.5)
                continue
            idle_strikes = 0
            fut = self.executor.submit(_dispatch, job)
            self.in_flight.add(fut)
            processed += 1
            self._reap()
        # drain remaining
        while self.in_flight:
            self._reap(blocking=True)
        self.processed += processed
        return processed

    def run_forever(self) -> None:
        """Block forever, draining the queue. Catch SIGINT/SIGTERM to exit cleanly."""
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        job_queue.reset_stuck_jobs()
        logger.info("Worker started (max_workers=%d)", self.max_workers)
        while not _shutdown.is_set():
            job = job_queue.claim_next_job()
            if job is None:
                self._reap()
                time.sleep(config.WORKER_IDLE_SLEEP_SEC)
                continue
            fut = self.executor.submit(_dispatch, job)
            self.in_flight.add(fut)
            self.processed += 1
            self._reap()
        logger.info("Worker draining %d in-flight jobs...", len(self.in_flight))
        while self.in_flight:
            self._reap(blocking=True)
        logger.info("Worker exited cleanly. Processed %d jobs.", self.processed)

    def _reap(self, *, blocking: bool = False) -> None:
        """Remove finished futures from the in-flight set."""
        if not self.in_flight:
            return
        if blocking:
            for fut in as_completed(list(self.in_flight)):
                try:
                    fut.result()
                except Exception:
                    pass
                self.in_flight.discard(fut)
                # After one, return so caller can decide
                return
        else:
            done = {f for f in self.in_flight if f.done()}
            for f in done:
                try:
                    f.result()
                except Exception:
                    pass
                self.in_flight.discard(f)


def run(*, once: bool = False, max_jobs: int | None = None) -> int:
    """Public entry point. Returns count of jobs processed."""
    worker = Worker()
    try:
        if once:
            return worker.run_once(max_jobs=max_jobs)
        worker.run_forever()
        return worker.processed
    finally:
        worker.shutdown()
