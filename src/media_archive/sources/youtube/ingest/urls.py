"""URL utilities for YouTube — recognition, normalization, ID extraction.

YouTube has more URL shapes than TikTok:
  - https://www.youtube.com/watch?v=VIDEO_ID
  - https://youtu.be/VIDEO_ID                       (short link)
  - https://m.youtube.com/watch?v=VIDEO_ID
  - https://www.youtube.com/shorts/VIDEO_ID         (Shorts)
  - https://www.youtube.com/embed/VIDEO_ID
  - https://www.youtube.com/v/VIDEO_ID              (legacy)
  - https://www.youtube.com/playlist?list=PLAYLIST_ID
  - https://www.youtube.com/@channel
  - https://www.youtube.com/channel/UC_CHANNEL_ID

For v0.2.0 we handle: single video URLs (all flavors above except
playlist/channel/@). Playlist ingestion is queued for v0.2.x; channel
subscription mirrors the TikTok creator-sync pattern and lands later.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlunparse

# Trailing-garbage trim shared across all source plugins. Same regex as
# the TikTok one, since the problem (zsh dquote artifacts, sentence
# punctuation, markdown parens) is shell-level not platform-level.
_TRAILING_GARBAGE_RE = re.compile(r"""[\s'"`)\],.>;:!?]+$""")

# Valid YouTube hosts. youtu.be is the short-link domain.
_YOUTUBE_HOSTS = (
    "www.youtube.com",
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
)

# YouTube video IDs are exactly 11 characters: A-Z, a-z, 0-9, dash, underscore.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Path patterns that contain a video ID. The order matters: more specific
# patterns first.
_PATH_VIDEO_ID_RE = re.compile(
    r"^/(?:shorts|embed|v|live)/([A-Za-z0-9_-]{11})(?:/|$)"
)


def is_youtube_url(url: str) -> bool:
    """Return True iff `url` parses as a YouTube host.

    Accepts youtube.com, youtu.be, m.youtube.com, music.youtube.com, and
    any *.youtube.com subdomain. Used as the first-line filter in bulk
    ingestion to reject non-YouTube URLs before we try to download.
    """
    if not url:
        return False
    cleaned = _TRAILING_GARBAGE_RE.sub("", url.strip())
    if not cleaned.startswith(("http://", "https://")):
        return False
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return False
    netloc = parsed.netloc.lower()
    return netloc in _YOUTUBE_HOSTS or netloc.endswith(".youtube.com")


def extract_video_id(url: str) -> str | None:
    """Pull the 11-character YouTube video ID out of any URL flavor.

    Returns None if the URL doesn't contain a recognizable video ID.
    Specifically rejects playlist-only URLs (no `v=` and no /watch path)
    since those need different handling — see is_playlist_url.
    """
    if not url:
        return None
    cleaned = _TRAILING_GARBAGE_RE.sub("", url.strip())
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return None

    netloc = parsed.netloc.lower()
    path = parsed.path or ""

    # youtu.be/VIDEO_ID — the path itself is the ID
    if netloc == "youtu.be":
        candidate = path.lstrip("/").split("/")[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    # /watch?v=VIDEO_ID — most common form
    if path == "/watch" or path.startswith("/watch/"):
        qs = parse_qs(parsed.query)
        v = (qs.get("v") or [""])[0]
        return v if _VIDEO_ID_RE.match(v) else None

    # /shorts/VIDEO_ID, /embed/VIDEO_ID, /v/VIDEO_ID, /live/VIDEO_ID
    m = _PATH_VIDEO_ID_RE.match(path)
    if m:
        return m.group(1)

    return None


def is_playlist_url(url: str) -> bool:
    """Return True iff this URL is a playlist (no individual video).

    We treat ?list=... + ?v=... as a "video with playlist context" — the
    video ID wins, and we ingest it as a single video. Pure playlist URLs
    (just /playlist?list=...) are not yet supported in v0.2.0.
    """
    if not url:
        return False
    cleaned = _TRAILING_GARBAGE_RE.sub("", url.strip())
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return False
    if parsed.path != "/playlist":
        return False
    qs = parse_qs(parsed.query)
    return bool(qs.get("list"))


def normalize_youtube_url(url: str) -> str:
    """Return a canonical https://www.youtube.com/watch?v=VIDEO_ID form.

    Strips tracking params (utm_*, si, feature, t, etc.), normalizes host
    (m.youtube.com → www.youtube.com), and converts short-links and
    Shorts URLs into the canonical /watch?v= form so dedup works correctly
    across flavors.

    URLs without a recognizable video ID are returned with trailing-garbage
    stripped but otherwise unchanged, so the caller can decide what to do.
    """
    if not url:
        return url
    cleaned = _TRAILING_GARBAGE_RE.sub("", url.strip())

    vid = extract_video_id(cleaned)
    if vid is None:
        return cleaned

    # Canonical form. Drop ALL query params except `v` to avoid leaking
    # tracking IDs like si= or utm_source= into the DB.
    return f"https://www.youtube.com/watch?v={vid}"
