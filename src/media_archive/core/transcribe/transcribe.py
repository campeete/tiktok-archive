"""
Audio extraction + Whisper transcription.

Two backends:
- mlx-whisper on Apple Silicon (uses Metal via MLX framework)
- faster-whisper elsewhere (CTranslate2-based, runs on CUDA or CPU)

Caller pattern:
  text, lang = transcribe_video_file(path)

If anything fails, raises a RuntimeError with a clear message.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from media_archive.core import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------

class NoAudioStreamError(RuntimeError):
    """ffmpeg ran fine but the input file had no audio stream.

    This usually means yt-dlp returned something that wasn't a real video
    (an HTML error page, a redirect, a malformed download). The transcribe
    stage can't recover from this; the caller should mark the post as
    failed and move on rather than retry.
    """


def extract_audio(video_path: Path, audio_path: Path | None = None) -> Path:
    """Extract a 16kHz mono WAV from any video file using ffmpeg.

    Returns the path to the extracted audio. If audio_path is None, places it
    next to the video with a .wav extension.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "ffmpeg not found on PATH. Install with: brew install ffmpeg"
        )

    if audio_path is None:
        audio_path = video_path.with_suffix(".wav")
    audio_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",  # overwrite
        "-loglevel", "error",
        "-i", str(video_path),
        "-vn",  # no video
        "-ac", "1",  # mono
        "-ar", "16000",  # 16kHz (whisper's native rate)
        "-c:a", "pcm_s16le",
        str(audio_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        # ffmpeg's signature for "you gave me a file with no audio
        # stream" is one of these phrases. Catch it so the caller can
        # report a clean error instead of bubbling the raw ffmpeg blob.
        no_audio_signatures = (
            "Output file does not contain any stream",
            "does not contain any stream",
            "Stream map '0:a' matches no streams",
            "Invalid data found when processing input",
        )
        if any(sig in stderr for sig in no_audio_signatures):
            raise NoAudioStreamError(
                "Downloaded file has no audio track. The fetcher likely "
                "returned an HTML error page or a malformed video. "
                f"(ffmpeg: {stderr[:200]})"
            )
        raise RuntimeError(f"ffmpeg failed: {stderr[:500]}")
    if not audio_path.exists():
        raise RuntimeError(f"ffmpeg succeeded but {audio_path} not found")
    return audio_path


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def _backend() -> str:
    """Return 'mlx' or 'faster' depending on platform/availability."""
    if config.IS_APPLE_SILICON:
        try:
            import mlx_whisper  # type: ignore  # noqa: F401
            return "mlx"
        except ImportError:
            pass
    try:
        import faster_whisper  # type: ignore  # noqa: F401
        return "faster"
    except ImportError:
        pass
    raise RuntimeError(
        "No Whisper backend available. Install one of:\n"
        "  pip install -e '.[analyze-mac]'  (Apple Silicon: mlx-whisper)\n"
        "  pip install -e '.[analyze-cuda]' (NVIDIA: faster-whisper)"
    )


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

# Map our friendly model name to backend-specific model identifiers
_MLX_MODEL_MAP = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large": "mlx-community/whisper-large-v3-mlx",
}


def transcribe_video_file(
    video_path: Path,
    *,
    language: str | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    """Transcribe a video. Returns (text, detected_language).

    Cleans up the intermediate .wav after transcription. The caller owns the
    .mp4 and is responsible for deleting it.
    """
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    model = model or config.WHISPER_MODEL
    language = language or config.WHISPER_LANGUAGE
    backend = _backend()

    # Extract audio to a tempfile so we can clean it up reliably
    with tempfile.TemporaryDirectory(prefix="tt-audio-") as tmpdir:
        audio_path = Path(tmpdir) / "audio.wav"
        extract_audio(video_path, audio_path)

        if backend == "mlx":
            return _transcribe_mlx(audio_path, model=model, language=language)
        else:
            return _transcribe_faster(audio_path, model=model, language=language)


def _transcribe_mlx(
    audio_path: Path, *, model: str, language: str
) -> tuple[str, str]:
    import mlx_whisper  # type: ignore

    repo = _MLX_MODEL_MAP.get(model, model)
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=repo,
        language=language if language and language != "auto" else None,
        verbose=False,
    )
    text = (result.get("text") or "").strip()
    detected = result.get("language") or language or ""
    return text, detected


def _transcribe_faster(
    audio_path: Path, *, model: str, language: str
) -> tuple[str, str]:
    from faster_whisper import WhisperModel  # type: ignore

    device = str(config.TORCH_DEVICE)
    compute_type = "float16" if device == "cuda" else "int8"
    wm = WhisperModel(model, device=device, compute_type=compute_type)
    segments, info = wm.transcribe(
        str(audio_path),
        language=language if language and language != "auto" else None,
        beam_size=5,
        vad_filter=True,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    detected = info.language or language or ""
    return text, detected


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def whisper_available() -> tuple[bool, str]:
    """Return (ok, backend_name_or_error) for diagnostics."""
    try:
        backend = _backend()
        return True, backend
    except RuntimeError as e:
        return False, str(e)
