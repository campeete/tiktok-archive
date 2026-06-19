"""
yt-dlp wrapper with rate limiting and retry behavior.

This is the ONLY place we shell out to yt-dlp. Everything else routes here so
rate limits, throttles, and bans are handled in one spot.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from media_archive.core import config

logger = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    success: bool
    file_path: Path | None = None
    info: dict | None = None
    error: str | None = None
    rate_limited: bool = False


# ---------------------------------------------------------------------------
# Global throttle
# ---------------------------------------------------------------------------
# Rate-limit state shared across workers in a single process. If we hit
# 429/403 from TikTok, we set _resume_after to now+RATE_LIMIT_PAUSE_SEC and
# every download blocks until that time.

_lock = threading.Lock()
_last_request_at: float = 0.0
_resume_after: float = 0.0


def _wait_for_rate_limit() -> None:
    """Sleep until we're allowed to make the next TikTok request."""
    global _last_request_at
    while True:
        with _lock:
            now = time.monotonic()
            # Global ban-pause check
            if now < _resume_after:
                wait = _resume_after - now
            else:
                # Per-request spacing
                spacing = config.YTDLP_SLEEP_INTERVAL
                elapsed = now - _last_request_at
                wait = max(0.0, spacing - elapsed)
            if wait <= 0:
                _last_request_at = now
                return
        time.sleep(min(wait, 5.0))  # wake up periodically to re-check


def _trigger_rate_limit_pause(reason: str) -> None:
    """Mark the global rate-limit cooldown."""
    global _resume_after
    with _lock:
        _resume_after = time.monotonic() + config.RATE_LIMIT_PAUSE_SEC
    logger.warning(
        "Rate limited by TikTok (%s). Pausing all downloads for %ds.",
        reason, config.RATE_LIMIT_PAUSE_SEC,
    )


# ---------------------------------------------------------------------------
# Single-URL download
# ---------------------------------------------------------------------------

def download_video(
    url: str,
    output_dir: Path,
    *,
    filename_template: str | None = None,
    timeout: int = 300,
) -> DownloadResult:
    """Download one TikTok video to output_dir. Returns DownloadResult.

    Filename template defaults to "{video_id}.{ext}" via yt-dlp's --output.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    template = filename_template or "%(id)s.%(ext)s"
    out_template = str(output_dir / template)

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--quiet",
        "--no-progress",
        "--no-playlist",
        "--dump-single-json",
        "--no-simulate",  # critical: --dump-single-json implies --simulate otherwise
        "--user-agent", config.YTDLP_USER_AGENT,
        "--sleep-interval", str(int(config.YTDLP_SLEEP_INTERVAL)),
        "--max-sleep-interval", str(int(config.YTDLP_MAX_SLEEP_INTERVAL)),
        "--retries", "3",
        "-o", out_template,
        url,
    ]

    _wait_for_rate_limit()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return DownloadResult(False, error=f"yt-dlp timed out after {timeout}s")
    except FileNotFoundError:
        return DownloadResult(
            False,
            error="yt-dlp not found on PATH. pip install -e '.[download]'",
        )

    stderr = proc.stderr or ""
    if proc.returncode != 0:
        rate_limited = (
            "HTTP Error 429" in stderr
            or "HTTP Error 403" in stderr
            or "Too Many Requests" in stderr
        )
        if rate_limited:
            _trigger_rate_limit_pause(stderr.splitlines()[0] if stderr else "unknown")
        return DownloadResult(
            False,
            error=f"yt-dlp exit {proc.returncode}: {stderr.strip()[:500]}",
            rate_limited=rate_limited,
        )

    # Parse the JSON metadata yt-dlp dumps to stdout
    try:
        info = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        info = {}

    # Find the actual file on disk
    file_path = _resolve_downloaded_file(output_dir, info)
    if file_path is None or not file_path.exists():
        return DownloadResult(
            False,
            error="yt-dlp succeeded but no video file found on disk",
            info=info,
        )

    return DownloadResult(
        success=True,
        file_path=file_path,
        info=info,
    )


def _resolve_downloaded_file(output_dir: Path, info: dict) -> Path | None:
    """yt-dlp may rename the output (codec/merge). Find the real file."""
    # Most reliable: yt-dlp tells us in requested_downloads
    requested = info.get("requested_downloads") or []
    for r in requested:
        fp = r.get("filepath") or r.get("filename") or r.get("_filename")
        if fp and Path(fp).exists():
            return Path(fp)

    # Fallback: scan output_dir for the most recent .mp4/.webm
    candidates = []
    for ext in (".mp4", ".webm", ".mkv", ".mov"):
        candidates.extend(output_dir.glob(f"*{ext}"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Profile crawl (creator sync)
# ---------------------------------------------------------------------------

def list_creator_videos(
    handle: str,
    *,
    max_videos: int | None = None,
    timeout: int = 600,
) -> tuple[list[dict], str | None]:
    """Use yt-dlp to enumerate videos on a creator's profile.

    Returns (entries, error). Each entry is a dict with at least 'url' and 'id'.
    Uses --flat-playlist for speed (no per-video metadata fetch).
    """
    profile_url = f"https://www.tiktok.com/@{handle.lstrip('@')}"
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--quiet",
        "--flat-playlist",
        "--dump-single-json",
        "--user-agent", config.YTDLP_USER_AGENT,
        "--sleep-interval", str(int(config.YTDLP_SLEEP_INTERVAL)),
        "--retries", "3",
    ]
    if max_videos is not None:
        cmd += ["--playlist-end", str(max_videos)]
    cmd.append(profile_url)

    _wait_for_rate_limit()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], f"yt-dlp profile crawl timed out after {timeout}s"
    except FileNotFoundError:
        return [], "yt-dlp not found on PATH"

    if proc.returncode != 0:
        stderr = proc.stderr or ""
        if "HTTP Error 429" in stderr or "HTTP Error 403" in stderr:
            _trigger_rate_limit_pause("profile crawl")
        return [], f"yt-dlp exit {proc.returncode}: {stderr.strip()[:500]}"

    try:
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError as e:
        return [], f"yt-dlp output not JSON: {e}"

    entries = data.get("entries") or []
    return entries, None


def yt_dlp_available() -> bool:
    """Return True iff yt-dlp is on PATH."""
    return shutil.which("yt-dlp") is not None


def yt_dlp_version() -> str | None:
    """Return yt-dlp version string or None."""
    if not yt_dlp_available():
        return None
    try:
        proc = subprocess.run(
            ["yt-dlp", "--version"], capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return None
