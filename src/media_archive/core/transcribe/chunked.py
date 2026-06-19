"""
Chunked Whisper transcription for long-form video (v0.2.0+).

Whisper handles audio up to ~30 minutes in a single pass before quality
degrades and memory usage spikes. For long-form content (podcasts,
lectures, hour-plus YouTube videos), we split the audio into overlapping
chunks, transcribe each chunk independently, and stitch the results back
together with timestamps.

Strategy:
  - For audio under SHORT_VIDEO_THRESHOLD (default 25 min), don't chunk.
    Just call transcribe_video_file directly. This keeps fast paths fast.
  - For longer audio, split into CHUNK_DURATION_SEC (default 1200 = 20min)
    chunks with CHUNK_OVERLAP_SEC (default 30) seconds of overlap.
  - Transcribe each chunk independently. Whisper's word/segment timestamps
    are relative to the chunk start; we shift them by the chunk's offset
    in the source audio.
  - Dedupe overlap text using a longest-common-substring trim at the
    join boundary. (Whisper is non-deterministic, so the overlap segment
    is rarely transcribed identically twice — we use Whisper's segment
    timestamps to align rather than text matching.)

Output:
  ChunkedTranscript(
    text="...full transcript...",
    language="en",
    duration_sec=4823.5,
    segments=[
      Segment(start=0.0, end=12.4, text="..."),
      Segment(start=12.4, end=25.1, text="..."),
      ...
    ],
    chunk_count=4,
  )

Segments are the raw Whisper-level segments (typically 5-15 sec each).
Higher-level summarization (every-5-min summaries, jump-to-timestamp UI)
is built on top of these in v0.3.0.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from media_archive.core import config
from media_archive.core.transcribe.transcribe import (
    NoAudioStreamError,
    extract_audio,
    transcribe_video_file,
    _backend,
    _transcribe_mlx,
    _transcribe_faster,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Skip chunking for anything under this length. Whisper handles 25min cleanly
# in one pass; the chunking overhead (extra ffmpeg calls, model re-init,
# segment merging) outweighs the benefit for short content.
SHORT_VIDEO_THRESHOLD_SEC = 25 * 60  # 1500 sec / 25 min

# Chunk size. Whisper's effective context is ~30 min before quality degrades;
# we use 20 to leave headroom and to give finer-grained progress reporting.
CHUNK_DURATION_SEC = 20 * 60  # 1200 sec / 20 min

# Overlap between adjacent chunks. When a sentence straddles a chunk
# boundary, we want both chunks to have enough context to transcribe it
# correctly, then we drop the duplicate at merge time.
CHUNK_OVERLAP_SEC = 30


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class TranscriptSegment:
    """A single Whisper segment with absolute timestamps in the source video."""

    start: float  # seconds from start of full video
    end: float
    text: str


@dataclass
class ChunkedTranscript:
    """The result of chunked transcription.

    `text` is the merged human-readable transcript (segments joined with
    spaces, overlap dedup applied). `segments` retains per-segment timing
    for downstream features like jump-to-timestamp.
    """

    text: str
    language: str
    duration_sec: float
    segments: list[TranscriptSegment] = field(default_factory=list)
    chunk_count: int = 1


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def probe_duration(media_path: Path) -> float:
    """Return the duration of the media file in seconds.

    Uses ffprobe (ships with ffmpeg). Returns 0.0 if probing fails so the
    caller can fall back to non-chunked transcription rather than crashing.
    """
    if not shutil.which("ffprobe"):
        logger.warning("ffprobe not found; cannot probe duration")
        return 0.0

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
        if proc.returncode != 0:
            logger.warning("ffprobe failed: %s", proc.stderr.strip()[:200])
            return 0.0
        return float(proc.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired) as e:
        logger.warning("ffprobe duration parse failed: %s", e)
        return 0.0


# ---------------------------------------------------------------------------
# Audio splitting
# ---------------------------------------------------------------------------

def _split_audio(
    audio_path: Path,
    *,
    chunk_duration: int,
    overlap: int,
    output_dir: Path,
) -> list[tuple[Path, float]]:
    """Split a WAV into overlapping chunks. Returns list of (path, offset_sec).

    offset_sec is the start position of the chunk in the original audio.
    Each chunk is its own .wav file under output_dir. Caller is responsible
    for cleanup.
    """
    duration = probe_duration(audio_path)
    if duration <= 0:
        # Probe failed; fall back to single chunk
        return [(audio_path, 0.0)]

    chunks: list[tuple[Path, float]] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step size = chunk_duration - overlap. If chunk_duration=1200 and
    # overlap=30, we advance 1170 sec per chunk. The last chunk may be
    # shorter than chunk_duration; that's fine.
    step = chunk_duration - overlap
    if step <= 0:
        raise ValueError(
            f"chunk_duration ({chunk_duration}) must exceed overlap ({overlap})"
        )

    offset = 0.0
    chunk_idx = 0
    while offset < duration:
        chunk_path = output_dir / f"chunk_{chunk_idx:03d}.wav"
        # ffmpeg seek + duration. -ss before -i is fast (input-level seek).
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel", "error",
            "-ss", f"{offset:.3f}",
            "-i", str(audio_path),
            "-t", f"{chunk_duration:.3f}",
            "-c", "copy",  # WAV is already PCM; no re-encode needed
            str(chunk_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not chunk_path.exists():
            raise RuntimeError(
                f"ffmpeg chunk split failed at offset {offset:.1f}s: "
                f"{proc.stderr.strip()[:300]}"
            )
        chunks.append((chunk_path, offset))
        offset += step
        chunk_idx += 1

    logger.info(
        "Split %.1fs audio into %d chunks (size=%ds, overlap=%ds)",
        duration, len(chunks), chunk_duration, overlap,
    )
    return chunks


# ---------------------------------------------------------------------------
# Per-chunk transcription with segment-level output
# ---------------------------------------------------------------------------

def _transcribe_chunk_with_segments(
    audio_path: Path,
    *,
    model: str,
    language: str,
) -> tuple[list[TranscriptSegment], str]:
    """Transcribe a single audio file and return per-segment timestamps.

    Unlike the v1.x transcribe_video_file (which returns a single concatenated
    string), this preserves Whisper's native segment boundaries with
    timestamps relative to the chunk's own t=0. The caller shifts these by
    the chunk's absolute offset.
    """
    backend = _backend()
    if backend == "mlx":
        return _segments_mlx(audio_path, model=model, language=language)
    else:
        return _segments_faster(audio_path, model=model, language=language)


def _segments_mlx(
    audio_path: Path, *, model: str, language: str
) -> tuple[list[TranscriptSegment], str]:
    import mlx_whisper  # type: ignore
    from media_archive.core.transcribe.transcribe import _MLX_MODEL_MAP

    repo = _MLX_MODEL_MAP.get(model, model)
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=repo,
        language=language if language and language != "auto" else None,
        verbose=False,
    )
    detected = result.get("language") or language or ""
    raw_segments = result.get("segments") or []
    segments: list[TranscriptSegment] = []
    for s in raw_segments:
        # mlx_whisper segment dict shape: {start, end, text, ...}
        text = (s.get("text") or "").strip()
        if not text:
            continue
        segments.append(TranscriptSegment(
            start=float(s.get("start", 0.0)),
            end=float(s.get("end", 0.0)),
            text=text,
        ))
    return segments, detected


def _segments_faster(
    audio_path: Path, *, model: str, language: str
) -> tuple[list[TranscriptSegment], str]:
    from faster_whisper import WhisperModel  # type: ignore

    device = str(config.TORCH_DEVICE)
    compute_type = "float16" if device == "cuda" else "int8"
    wm = WhisperModel(model, device=device, compute_type=compute_type)
    segs, info = wm.transcribe(
        str(audio_path),
        language=language if language and language != "auto" else None,
        beam_size=5,
        vad_filter=True,
    )
    detected = info.language or language or ""
    segments: list[TranscriptSegment] = []
    for s in segs:
        text = s.text.strip()
        if not text:
            continue
        segments.append(TranscriptSegment(
            start=float(s.start),
            end=float(s.end),
            text=text,
        ))
    return segments, detected


# ---------------------------------------------------------------------------
# Merging chunked output
# ---------------------------------------------------------------------------

def _merge_chunks(
    chunk_results: list[tuple[list[TranscriptSegment], float]],
    *,
    overlap: int,
) -> list[TranscriptSegment]:
    """Merge per-chunk segments into a single timeline, deduping overlap.

    chunk_results is [(segments, chunk_offset), ...] in source-order.

    Approach: for each chunk after the first, drop any segments whose start
    timestamp (after offset adjustment) is within `overlap` seconds of the
    previous chunk's last segment end. This is a coarse dedupe — it doesn't
    handle the case where Whisper transcribed the overlap region differently
    in the two chunks, but it avoids stitching duplicate sentences.

    We rely on Whisper's VAD/segment boundaries being roughly stable across
    runs of the same audio; the overlap region typically yields the same
    segment count from both sides, so dropping the early-arriving copies
    from the later chunk gives a clean merge.
    """
    merged: list[TranscriptSegment] = []
    last_end = -1.0  # absolute time of last accepted segment's end

    for segs, offset in chunk_results:
        for s in segs:
            abs_start = s.start + offset
            abs_end = s.end + offset
            # Drop segments that fall entirely within the previous chunk's
            # tail (i.e. inside the overlap window we already covered).
            if abs_start < last_end - 0.5:  # 0.5s tolerance for rounding
                continue
            merged.append(TranscriptSegment(
                start=abs_start,
                end=abs_end,
                text=s.text,
            ))
            last_end = max(last_end, abs_end)

    return merged


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def chunked_transcribe_video_file(
    video_path: Path,
    *,
    language: str | None = None,
    model: str | None = None,
    short_threshold_sec: int = SHORT_VIDEO_THRESHOLD_SEC,
    chunk_duration_sec: int = CHUNK_DURATION_SEC,
    overlap_sec: int = CHUNK_OVERLAP_SEC,
) -> ChunkedTranscript:
    """Transcribe a video, chunking the audio if it's long-form.

    For videos under `short_threshold_sec`, this delegates to
    `transcribe_video_file` (the existing fast path) and wraps the result
    in a ChunkedTranscript with a single synthesized segment covering the
    whole transcript. This keeps the short-video pipeline behavior
    identical to v1.7.x.

    For longer videos, audio is split into overlapping chunks, each
    transcribed independently with segment-level timestamps, then merged
    into a unified timeline.

    Cleans up all intermediate audio files. The caller owns the .mp4 and
    is responsible for deleting it.

    Raises NoAudioStreamError if ffmpeg can't find audio. Raises RuntimeError
    on other failures.
    """
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    model = model or config.WHISPER_MODEL
    language = language or config.WHISPER_LANGUAGE

    # Probe duration to decide whether to chunk. Probing the source video
    # is cheaper than extracting the full audio first.
    duration = probe_duration(video_path)

    if duration <= 0:
        # Probe failed (rare — usually a malformed file). Fall back to
        # the v1.x single-pass transcribe and wrap the output.
        logger.warning(
            "Could not probe duration of %s; using single-pass transcription",
            video_path,
        )
        text, lang = transcribe_video_file(video_path, language=language, model=model)
        return ChunkedTranscript(
            text=text,
            language=lang,
            duration_sec=0.0,
            segments=[TranscriptSegment(start=0.0, end=0.0, text=text)] if text else [],
            chunk_count=1,
        )

    if duration < short_threshold_sec:
        # Short content. Use the fast path. Wrap as a single-segment chunked
        # result for API consistency.
        logger.info(
            "Short video (%.1fs < %ds); using single-pass transcription",
            duration, short_threshold_sec,
        )
        text, lang = transcribe_video_file(video_path, language=language, model=model)
        return ChunkedTranscript(
            text=text,
            language=lang,
            duration_sec=duration,
            segments=[TranscriptSegment(start=0.0, end=duration, text=text)] if text else [],
            chunk_count=1,
        )

    # Long-form path. Extract full audio once, split into chunks,
    # transcribe each, merge.
    logger.info(
        "Long video (%.1fs >= %ds); chunked transcription",
        duration, short_threshold_sec,
    )

    with tempfile.TemporaryDirectory(prefix="ma-chunked-") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        full_audio = tmpdir / "full.wav"
        extract_audio(video_path, full_audio)  # raises NoAudioStreamError if no audio

        chunks_dir = tmpdir / "chunks"
        chunks = _split_audio(
            full_audio,
            chunk_duration=chunk_duration_sec,
            overlap=overlap_sec,
            output_dir=chunks_dir,
        )

        chunk_results: list[tuple[list[TranscriptSegment], float]] = []
        detected_lang = ""
        for i, (chunk_path, offset) in enumerate(chunks, 1):
            logger.info(
                "Transcribing chunk %d/%d (offset=%.1fs)", i, len(chunks), offset,
            )
            segs, lang = _transcribe_chunk_with_segments(
                chunk_path, model=model, language=language,
            )
            chunk_results.append((segs, offset))
            if lang and not detected_lang:
                detected_lang = lang

        merged = _merge_chunks(chunk_results, overlap=overlap_sec)
        full_text = " ".join(s.text for s in merged).strip()

        return ChunkedTranscript(
            text=full_text,
            language=detected_lang or (language or ""),
            duration_sec=duration,
            segments=merged,
            chunk_count=len(chunks),
        )
