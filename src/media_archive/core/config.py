"""
Cross-platform configuration for tiktok-archive.

All paths and tunables live here. Read once at startup; do not mutate at runtime.

Environment variables override defaults. See .env.example for the full list.
"""
from __future__ import annotations

import os
import platform as _platform
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# .env loading (no external deps, manual parser)
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Idempotent. Does not overwrite existing env vars (so explicit env beats .env).
    Silently ignores comments, blank lines, and malformed entries.
    """
    if not path.is_file():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except (OSError, UnicodeDecodeError):
        pass


# ---------------------------------------------------------------------------
# Project root resolution
# ---------------------------------------------------------------------------

# config.py lives at src/media_archive/core/config.py — project root is THREE parents up
# (.../core -> .../media_archive -> .../src -> .../<project root>).
# This was two parents up in v1.7.x when config.py lived at src/tiktok_archive/config.py;
# the v0.1.0 reorg added one nesting level.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent.parent

# Load .env BEFORE reading any of the env-driven settings below
_load_dotenv(PROJECT_ROOT / ".env")


def _env_path(key: str, default: Path) -> Path:
    val = os.environ.get(key)
    return Path(val).expanduser() if val else default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    try:
        return int(val) if val else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    try:
        return float(val) if val else default
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_str(key: str, default: str) -> str:
    val = os.environ.get(key)
    return val if val else default


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

PLATFORM = _platform.system().lower()  # "darwin", "linux", "windows"
ARCH = _platform.machine().lower()  # "arm64", "x86_64", etc
IS_APPLE_SILICON = PLATFORM == "darwin" and ARCH in {"arm64", "aarch64"}
IS_LINUX = PLATFORM == "linux"
IS_WINDOWS = PLATFORM == "windows"


def _detect_torch_device() -> str:
    """Return 'cuda', 'mps', or 'cpu' depending on what's actually available.

    Imported lazily to avoid pulling torch into every CLI invocation.
    """
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            return "cuda"
        if (
            getattr(torch.backends, "mps", None) is not None
            and torch.backends.mps.is_available()
        ):
            return "mps"
    except (ImportError, RuntimeError, AttributeError):
        pass
    return "cpu"


# Lazy attribute access pattern for torch device — only resolved on first read.
class _LazyTorchDevice:
    _value: str | None = None

    def __str__(self) -> str:
        if self._value is None:
            self._value = _detect_torch_device()
        return self._value

    def __eq__(self, other: object) -> bool:
        return str(self) == other


TORCH_DEVICE = _LazyTorchDevice()


# ---------------------------------------------------------------------------
# Data layout
# ---------------------------------------------------------------------------

DATA_DIR: Path = _env_path("TT_DATA_DIR", PROJECT_ROOT / "data")

# Scratch: where videos live briefly during transcription, then deleted.
# Default to a subdirectory of DATA_DIR for portability; can override to
# external drive or /tmp if user wants.
SCRATCH_DIR: Path = _env_path("TT_SCRATCH_DIR", DATA_DIR / "scratch")

# Transcripts: durable JSON files, one per video. This IS the asset.
TRANSCRIPTS_DIR: Path = _env_path("TT_TRANSCRIPTS_DIR", DATA_DIR / "transcripts")

# Media artifacts (Phase 1.7): per-video and per-photo image artifacts.
# Layout: <MEDIA_DIR>/<video_id>/{thumb-NN.jpg,frame-NN.jpg,slide-NN-thumb.jpg,slide-NN.jpg}
MEDIA_DIR: Path = _env_path("TT_MEDIA_DIR", DATA_DIR / "media")

# Playwright persistent profile (Phase 1.7.1). Holds cookies/localStorage
# across runs so we can fetch authenticated TikTok pages without
# re-logging-in every time. Created on demand by `tiktok-archive auth-tiktok`.
BROWSER_PROFILE_DIR: Path = _env_path(
    "TT_BROWSER_PROFILE_DIR", DATA_DIR / "playwright-profile"
)

# DB backups: local copies of nightly SQLite snapshots before R2 upload.
DB_BACKUPS_DIR: Path = _env_path("TT_DB_BACKUPS_DIR", DATA_DIR / "db-backups")

# ChromaDB embeddings (Phase 5, but reserve the dir).
CHROMA_DIR: Path = _env_path("TT_CHROMA_DIR", DATA_DIR / "chroma")

DB_PATH: Path = _env_path("TT_DB_PATH", DATA_DIR / "tiktok.db")
DB_URL: str = _env_str("TT_DB_URL", f"sqlite:///{DB_PATH}")

# Logs
LOG_DIR: Path = _env_path("TT_LOG_DIR", DATA_DIR / "logs")
LOG_LEVEL: str = _env_str("TT_LOG_LEVEL", "INFO").upper()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

WHISPER_MODEL: str = _env_str("TT_WHISPER_MODEL", "medium")
WHISPER_LANGUAGE: str = _env_str("TT_WHISPER_LANGUAGE", "en")
EMBEDDING_MODEL: str = _env_str("TT_EMBEDDING_MODEL", "nomic-embed-text")
TAG_MODEL: str = _env_str("TT_TAG_MODEL", "qwen2.5:7b")
SUMMARY_MODEL: str = _env_str("TT_SUMMARY_MODEL", "qwen2.5:7b")
QA_MODEL: str = _env_str("TT_QA_MODEL", "qwen2.5:7b")
OLLAMA_HOST: str = _env_str("TT_OLLAMA_HOST", "http://localhost:11434")


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

WEB_HOST: str = _env_str("TT_WEB_HOST", "127.0.0.1")
WEB_PORT: int = _env_int("TT_WEB_PORT", 5050)


# ---------------------------------------------------------------------------
# Job queue + worker
# ---------------------------------------------------------------------------

# How many concurrent workers per stage. Tuned for an M-series Mac.
# Whisper is single-threaded so 1 is correct. Ollama can serve 2 calls.
# yt-dlp must be 1 for rate limiting safety.
WORKER_DOWNLOAD_CONCURRENCY: int = _env_int("TT_WORKER_DOWNLOAD_CONCURRENCY", 1)
WORKER_TRANSCRIBE_CONCURRENCY: int = _env_int("TT_WORKER_TRANSCRIBE_CONCURRENCY", 1)
WORKER_TAG_CONCURRENCY: int = _env_int("TT_WORKER_TAG_CONCURRENCY", 2)
WORKER_EMBED_CONCURRENCY: int = _env_int("TT_WORKER_EMBED_CONCURRENCY", 2)

# Job retry + backoff
JOB_MAX_ATTEMPTS: int = _env_int("TT_JOB_MAX_ATTEMPTS", 3)
JOB_BACKOFF_BASE_SEC: float = _env_float("TT_JOB_BACKOFF_BASE_SEC", 30.0)
JOB_BACKOFF_FACTOR: float = _env_float("TT_JOB_BACKOFF_FACTOR", 3.0)
JOB_LOCK_TIMEOUT_SEC: int = _env_int("TT_JOB_LOCK_TIMEOUT_SEC", 1800)  # 30 min

# Worker poll interval when queue is empty
WORKER_IDLE_SLEEP_SEC: float = _env_float("TT_WORKER_IDLE_SLEEP_SEC", 5.0)


# ---------------------------------------------------------------------------
# yt-dlp rate limiting (TikTok will ban you if you hammer it)
# ---------------------------------------------------------------------------

YTDLP_SLEEP_INTERVAL: float = _env_float("TT_YTDLP_SLEEP_INTERVAL", 3.0)
YTDLP_MAX_SLEEP_INTERVAL: float = _env_float("TT_YTDLP_MAX_SLEEP_INTERVAL", 6.0)
YTDLP_USER_AGENT: str = _env_str(
    "TT_YTDLP_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
# When we hit a 429/403, pause this long before retrying any TikTok request
RATE_LIMIT_PAUSE_SEC: int = _env_int("TT_RATE_LIMIT_PAUSE_SEC", 3600)


# ---------------------------------------------------------------------------
# Creator sync defaults
# ---------------------------------------------------------------------------

# Per-creator first-sync depth: full | last-6mo | last-50
CREATOR_DEFAULT_DEPTH: str = _env_str("TT_CREATOR_DEFAULT_DEPTH", "last-6mo")
# How often to re-poll creators (in hours)
CREATOR_SYNC_INTERVAL_HOURS: int = _env_int("TT_CREATOR_SYNC_INTERVAL_HOURS", 24)


# ---------------------------------------------------------------------------
# Storage backend (durable transcripts/metadata)
# ---------------------------------------------------------------------------

# "local" or "r2" — determines whether transcripts get mirrored to cloud.
# Local copy is always primary; R2 is a durable backup when enabled.
STORAGE_BACKEND: str = _env_str("TT_STORAGE_BACKEND", "local").lower()

R2_ACCOUNT_ID: str = _env_str("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID: str = _env_str("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY: str = _env_str("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME: str = _env_str("R2_BUCKET_NAME", "tiktok-archive")
R2_ENDPOINT: str = _env_str("R2_ENDPOINT", "")  # auto-derived if blank


def r2_endpoint_url() -> str:
    """Return the configured R2 endpoint, deriving from account ID if blank."""
    if R2_ENDPOINT:
        return R2_ENDPOINT
    if R2_ACCOUNT_ID:
        return f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return ""


def r2_configured() -> bool:
    """Return True iff all R2 settings are populated."""
    return all([
        STORAGE_BACKEND == "r2",
        R2_ACCOUNT_ID,
        R2_ACCESS_KEY_ID,
        R2_SECRET_ACCESS_KEY,
        R2_BUCKET_NAME,
    ])


# Backup retention: keep the last N daily DB backups in R2
DB_BACKUP_RETENTION_DAYS: int = _env_int("TT_DB_BACKUP_RETENTION_DAYS", 30)


# ---------------------------------------------------------------------------
# Tag vocabulary
# ---------------------------------------------------------------------------

TAG_VOCABULARY_PATH: Path = _env_path(
    "TT_TAG_VOCABULARY_PATH", PROJECT_ROOT / "tags_vocabulary.yaml"
)
# If the user's vocabulary file is missing, fall back to the example shipped
# with the repo so the system still functions out of the box.
TAG_VOCABULARY_FALLBACK_PATH: Path = (
    PROJECT_ROOT / "tags_vocabulary.example.yaml"
)


# ---------------------------------------------------------------------------
# Creators config
# ---------------------------------------------------------------------------

CREATORS_PATH: Path = _env_path("TT_CREATORS_PATH", PROJECT_ROOT / "creators.yaml")


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    """Create all data directories if they don't exist. Safe to call repeatedly."""
    for d in (
        DATA_DIR,
        SCRATCH_DIR,
        TRANSCRIPTS_DIR,
        MEDIA_DIR,
        DB_BACKUPS_DIR,
        CHROMA_DIR,
        LOG_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def diagnostic_dict() -> dict:
    """Return a flat dict useful for the `check` and `stats` commands."""
    return {
        "python": sys.version.split()[0],
        "platform": f"{_platform.system()} ({_platform.machine()})",
        "apple_silicon": IS_APPLE_SILICON,
        "torch_device": str(TORCH_DEVICE),
        "project_root": str(PROJECT_ROOT),
        "db_path": str(DB_PATH),
        "scratch_dir": str(SCRATCH_DIR),
        "transcripts_dir": str(TRANSCRIPTS_DIR),
        "media_dir": str(MEDIA_DIR),
        "db_backups_dir": str(DB_BACKUPS_DIR),
        "whisper_model": WHISPER_MODEL,
        "embedding_model": EMBEDDING_MODEL,
        "tag_model": TAG_MODEL,
        "ollama_host": OLLAMA_HOST,
        "storage_backend": STORAGE_BACKEND,
        "r2_configured": r2_configured(),
        "r2_bucket": R2_BUCKET_NAME if r2_configured() else "(not configured)",
        "creators_path": str(CREATORS_PATH),
        "worker_concurrency": {
            "download": WORKER_DOWNLOAD_CONCURRENCY,
            "transcribe": WORKER_TRANSCRIBE_CONCURRENCY,
            "tag": WORKER_TAG_CONCURRENCY,
            "embed": WORKER_EMBED_CONCURRENCY,
        },
    }
