"""
Media artifact extraction (Phase 1.7).

Two responsibilities:
1. Pull representative still images out of source media.
   - Videos: uniform every-2s thumbnails for everything, plus full-res
     scene-change frames when the post is marked important.
   - Photo posts: thumbnails of every slide, plus full-res slides when
     the post is marked important OR transcript is empty.
2. Persist those images to disk + R2 and record them in MediaArtifact.

The "is this post important enough to keep full-res?" judgment is made
elsewhere (importance.py for the LLM judge, creators.yaml for the manual
override, the empty-transcript auto-rule fires inline below). This
module just takes that boolean and does the work.

Dependencies:
- ffmpeg (already installed for transcribe pipeline) — uniform sampling
- Pillow — thumbnailing + image format conversion
- scenedetect (optional) — scene-change frame extraction. If missing,
  scene-change frames are silently skipped and the post falls back to
  uniform-only behavior.

Failure mode: any extraction error is logged and the operation continues.
We never let a bad video kill the whole pipeline. If frames fail, the
transcript is still the canonical artifact.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from media_archive.core import config
from media_archive.core.db.schemas import MediaArtifact, Video, session_scope
from media_archive.core.storage import make_storage

logger = logging.getLogger(__name__)


# Thumbnail width target (px). 256 is wide enough to read at a glance,
# small enough that even hundreds fit on a detail page without thrashing.
THUMB_WIDTH = 256

# JPEG quality for both thumbnails and full-res. 85 is the sweet spot
# between visible quality and size; full-res slides come from TikTok's
# CDN already JPEG-encoded so we're not double-encoding much.
JPEG_QUALITY = 85

# How often to sample uniform video frames. 2.0s gives ~5 frames per
# 10s clip and ~30 frames per minute; small enough to be cheap on disk
# even if we run it on the whole archive.
UNIFORM_INTERVAL_SEC = 2.0

# PySceneDetect threshold. The library's docs say 27.0 is a reasonable
# default for content-aware detection; we override only if necessary.
SCENE_THRESHOLD = 27.0


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class MediaExtractionResult:
    """What got produced for one video/photo. Returned for logging only —
    the canonical record is the MediaArtifact rows in the DB."""
    video_id: int
    thumbs_created: int = 0
    full_frames_created: int = 0
    error: str | None = None

    @property
    def total(self) -> int:
        return self.thumbs_created + self.full_frames_created


# ---------------------------------------------------------------------------
# Pillow lazy-load helpers (Pillow may not be installed yet)
# ---------------------------------------------------------------------------

def _pillow_available() -> bool:
    """Whether Pillow is importable. Used in `check` and to short-circuit
    operations that need to thumbnail."""
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


def _scenedetect_available() -> bool:
    """Whether PySceneDetect is importable."""
    try:
        from scenedetect import detect, ContentDetector  # noqa: F401
        return True
    except ImportError:
        return False


def _thumbnail_image(src: Path, dst: Path, width: int = THUMB_WIDTH) -> tuple[int, int] | None:
    """Resize an image to `width` px wide, preserving aspect, save as JPEG.

    Returns (width, height) of the saved thumbnail, or None on failure.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed; cannot thumbnail %s", src.name)
        return None

    try:
        with Image.open(src) as im:
            im = im.convert("RGB")  # JPEG can't store RGBA
            ratio = width / im.width
            new_h = max(1, int(im.height * ratio))
            im = im.resize((width, new_h), Image.LANCZOS)
            im.save(dst, "JPEG", quality=JPEG_QUALITY, optimize=True)
            return (width, new_h)
    except Exception as e:
        logger.warning("Could not thumbnail %s: %s", src.name, e)
        return None


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Read width/height from an existing image file."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (None, None)


# ---------------------------------------------------------------------------
# ffmpeg-driven frame extraction
# ---------------------------------------------------------------------------

def _extract_uniform_frames(
    video_path: Path,
    output_dir: Path,
    interval_sec: float = UNIFORM_INTERVAL_SEC,
) -> list[tuple[Path, float]]:
    """Sample frames at fixed intervals using ffmpeg.

    Output filenames: thumb-source-001.jpg, thumb-source-002.jpg, ...
    These are full-res sources; the thumbnail step downsizes them after.

    Returns [(path, timestamp_sec), ...] for successfully written frames,
    or [] if ffmpeg fails.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "thumb-source-%03d.jpg"

    # fps filter: 1/interval gives one frame every interval_sec.
    # qscale 2 is high-quality JPEG output from ffmpeg (1=best, 31=worst).
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", str(video_path),
        "-vf", f"fps=1/{interval_sec}",
        "-qscale:v", "2",
        str(pattern),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.warning("ffmpeg uniform-sample failed for %s: %s",
                          video_path.name, (result.stderr or "")[:200])
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("ffmpeg call failed: %s", e)
        return []

    frames: list[tuple[Path, float]] = []
    for i, p in enumerate(sorted(output_dir.glob("thumb-source-*.jpg"))):
        # Compute timestamp from index. Approximate but accurate enough
        # for "where in the video" UI display.
        ts = i * interval_sec
        frames.append((p, ts))
    return frames


def _extract_scene_change_frames(
    video_path: Path,
    output_dir: Path,
    threshold: float = SCENE_THRESHOLD,
) -> list[tuple[Path, float]]:
    """Detect scene cuts and write a frame at each cut.

    Returns [(path, timestamp_sec), ...] or [] if scenedetect is missing
    or detection fails.
    """
    if not _scenedetect_available():
        logger.info("PySceneDetect not installed; skipping scene-change frames")
        return []

    try:
        from scenedetect import detect, ContentDetector
    except ImportError:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        scenes = detect(str(video_path), ContentDetector(threshold=threshold))
    except Exception as e:
        logger.warning("Scene detection failed for %s: %s", video_path.name, e)
        return []

    if not scenes:
        return []

    # For each scene, ffmpeg-extract one frame at the scene's start.
    out_paths: list[tuple[Path, float]] = []
    for i, (start, _end) in enumerate(scenes, 1):
        ts_sec = start.get_seconds()
        out = output_dir / f"frame-source-{i:03d}.jpg"
        cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-ss", str(ts_sec),
            "-i", str(video_path),
            "-frames:v", "1",
            "-qscale:v", "2",
            str(out),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and out.exists():
                out_paths.append((out, ts_sec))
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg timeout extracting scene frame %d", i)
            continue
    return out_paths


# ---------------------------------------------------------------------------
# Image download (for photo-post slides) — uses requests directly so we
# don't need to import the storage layer for this read-only fetch.
# ---------------------------------------------------------------------------

def _download_image_url(url: str, dst: Path, timeout: int = 30) -> bool:
    """Fetch an image by URL into dst. Returns True on success."""
    import requests
    try:
        # Browser-like headers — TikTok's CDN sometimes 403s default
        # python-requests user-agents.
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
                "Safari/537.36"
            ),
            "Accept": "image/webp,image/jpeg,image/*;q=0.9,*/*;q=0.5",
            "Referer": "https://www.tiktok.com/",
        }
        r = requests.get(url, headers=headers, timeout=timeout, stream=True)
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
        return dst.stat().st_size > 0
    except Exception as e:
        logger.warning("Could not download %s: %s", url, e)
        return False


# ---------------------------------------------------------------------------
# Persistence — writes both local and R2, records MediaArtifact rows
# ---------------------------------------------------------------------------

def _persist_artifact(
    *,
    video_id: int,
    kind: str,
    sequence: int,
    src_path: Path,
    timestamp_sec: float | None,
) -> MediaArtifact | None:
    """Move src_path into the durable media tree, mirror to R2, record the
    row. Returns the saved MediaArtifact or None on failure.
    """
    if not src_path.exists():
        return None

    # Local destination: data/media/<video_id>/<kind>-<seq>.jpg
    local_dir = config.MEDIA_DIR / str(video_id)
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / f"{kind}-{sequence:03d}.jpg"
    try:
        shutil.move(str(src_path), str(local_path))
    except Exception as e:
        logger.warning("Could not move %s to %s: %s", src_path, local_path, e)
        return None

    size_bytes = local_path.stat().st_size
    width, height = _image_dimensions(local_path)

    # R2 mirror: media/<video_id>/<kind>-<seq>.jpg
    r2_key: str | None = None
    try:
        storage = make_storage()
        key = f"media/{video_id}/{kind}-{sequence:03d}.jpg"
        with open(local_path, "rb") as f:
            data = f.read()
        storage.put(key, data)
        # storage.put may return a key (R2) or just confirm (local).
        # We only set r2_key if we're actually using R2.
        if config.STORAGE_BACKEND == "r2":
            r2_key = key
    except Exception as e:
        # R2 sync is best-effort; missing R2 sync should not block local save.
        logger.debug("R2 mirror skipped for %s: %s", local_path.name, e)

    # Record row. Use ON CONFLICT-style upsert: if we re-extract for this
    # video (e.g., after marking it important), drop the old row first.
    with session_scope() as s:
        existing = (
            s.query(MediaArtifact)
            .filter_by(video_id=video_id, kind=kind, sequence=sequence)
            .one_or_none()
        )
        if existing:
            existing.local_path = str(local_path)
            existing.r2_key = r2_key
            existing.size_bytes = size_bytes
            existing.width = width
            existing.height = height
            existing.timestamp_sec = timestamp_sec
            s.flush()
            s.expunge(existing)
            return existing

        artifact = MediaArtifact(
            video_id=video_id,
            kind=kind,
            sequence=sequence,
            timestamp_sec=timestamp_sec,
            local_path=str(local_path),
            r2_key=r2_key,
            size_bytes=size_bytes,
            width=width,
            height=height,
        )
        s.add(artifact)
        s.flush()
        s.expunge(artifact)
        return artifact


# ---------------------------------------------------------------------------
# Public API: video frames
# ---------------------------------------------------------------------------

def extract_video_frames(
    video_id: int,
    video_path: Path,
    *,
    is_important: bool,
) -> MediaExtractionResult:
    """Extract uniform thumbnails (always) and scene-change full frames
    (only if important) from a video file.

    Stores both locally and in R2 if configured. Records rows in
    MediaArtifact.
    """
    result = MediaExtractionResult(video_id=video_id)
    if not video_path.is_file():
        result.error = f"Video file does not exist: {video_path}"
        return result

    if not _pillow_available():
        result.error = "Pillow not installed; cannot produce thumbnails"
        return result

    with tempfile.TemporaryDirectory(prefix="tt-frames-") as tmpdir:
        tmp = Path(tmpdir)

        # Uniform thumbs — always
        uniform_dir = tmp / "uniform"
        sources = _extract_uniform_frames(video_path, uniform_dir)
        for i, (src, ts) in enumerate(sources):
            thumb = tmp / f"thumb-{i:03d}.jpg"
            dims = _thumbnail_image(src, thumb)
            if dims is None:
                continue
            saved = _persist_artifact(
                video_id=video_id,
                kind="frame_thumb",
                sequence=i,
                src_path=thumb,
                timestamp_sec=ts,
            )
            if saved is not None:
                result.thumbs_created += 1

        # Scene-change full frames — only if important
        if is_important:
            scene_dir = tmp / "scenes"
            scenes = _extract_scene_change_frames(video_path, scene_dir)
            for i, (src, ts) in enumerate(scenes):
                # No thumbnailing — these are kept full-res by design
                saved = _persist_artifact(
                    video_id=video_id,
                    kind="frame_full",
                    sequence=i,
                    src_path=src,
                    timestamp_sec=ts,
                )
                if saved is not None:
                    result.full_frames_created += 1

    return result


# ---------------------------------------------------------------------------
# Public API: photo slides
# ---------------------------------------------------------------------------

def extract_photo_slides(
    video_id: int,
    image_urls: Iterable[str],
    *,
    is_important: bool,
) -> MediaExtractionResult:
    """Download each photo-post slide, save thumbnail (always) and
    full-res (only if important).

    image_urls must be in original post order — the position becomes the
    sequence field on each MediaArtifact.
    """
    result = MediaExtractionResult(video_id=video_id)

    if not _pillow_available():
        result.error = "Pillow not installed; cannot thumbnail slides"
        return result

    urls = list(image_urls)
    if not urls:
        return result

    with tempfile.TemporaryDirectory(prefix="tt-slides-") as tmpdir:
        tmp = Path(tmpdir)

        for i, url in enumerate(urls):
            full_src = tmp / f"slide-source-{i:03d}.jpg"
            if not _download_image_url(url, full_src):
                continue

            # Thumbnail — always
            thumb = tmp / f"slide-thumb-{i:03d}.jpg"
            if _thumbnail_image(full_src, thumb) is not None:
                saved = _persist_artifact(
                    video_id=video_id,
                    kind="slide_thumb",
                    sequence=i,
                    src_path=thumb,
                    timestamp_sec=None,
                )
                if saved is not None:
                    result.thumbs_created += 1

            # Full-res — only if important
            if is_important:
                # Move the source itself; we already downloaded it once
                full_keep = tmp / f"slide-full-{i:03d}.jpg"
                # Copy because full_src may already be moved by thumb step
                if full_src.exists():
                    shutil.copy(str(full_src), str(full_keep))
                    saved = _persist_artifact(
                        video_id=video_id,
                        kind="slide_full",
                        sequence=i,
                        src_path=full_keep,
                        timestamp_sec=None,
                    )
                    if saved is not None:
                        result.full_frames_created += 1

    return result


# ---------------------------------------------------------------------------
# Re-extraction (called when user marks/unmarks important from the UI)
# ---------------------------------------------------------------------------

def drop_full_artifacts(video_id: int) -> int:
    """Remove all `frame_full` and `slide_full` artifacts for a video.

    Called when a video is unmarked important. The thumbnails stay (they
    were always going to be kept). Returns count of deleted rows.
    """
    deleted = 0
    with session_scope() as s:
        rows = (
            s.query(MediaArtifact)
            .filter(
                MediaArtifact.video_id == video_id,
                MediaArtifact.kind.in_(["frame_full", "slide_full"]),
            )
            .all()
        )
        for row in rows:
            if row.local_path:
                try:
                    Path(row.local_path).unlink(missing_ok=True)
                except Exception as e:
                    logger.debug("Could not delete %s: %s", row.local_path, e)
            if row.r2_key:
                try:
                    storage = make_storage()
                    storage.delete(row.r2_key)
                except Exception as e:
                    logger.debug("Could not delete R2 key %s: %s", row.r2_key, e)
            s.delete(row)
            deleted += 1
    return deleted
