"""Utilities for normalizing TikTok URLs and extracting video / photo IDs.

TikTok hosts two relevant post types under similar URL shapes:
  https://www.tiktok.com/@handle/video/1234567890   -- normal short video
  https://www.tiktok.com/@handle/photo/1234567890   -- photo carousel post

We treat both uniformly throughout the pipeline; the post type is recorded
on the Video row's source/notes for analytics, but the natural key is still
URL+source+collection.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

# Matches both /video/ and /photo/ paths.
_POST_PATH_RE = re.compile(r"/@([^/]+)/(video|photo)/(\d+)")

# Recognized TikTok hosts. We accept m.tiktok.com and bare tiktok.com,
# normalizing both up to www.tiktok.com.
_TIKTOK_HOSTS = ("www.tiktok.com", "tiktok.com", "m.tiktok.com", "vm.tiktok.com")

# Punctuation we'll strip from URL tails. Shells (especially zsh under
# dquote> mode) and chat clients sometimes leave trailing quotes,
# parens, commas, or periods that real URLs don't have.
_TRAILING_GARBAGE_RE = re.compile(r"""[\s'"`)\],.>;:!?]+$""")


def is_tiktok_url(url: str) -> bool:
    """Return True iff `url` parses as a TikTok host.

    Used to reject non-TikTok URLs (YouTube, Instagram, etc.) before
    we try to download them. Without this guard, yt-dlp will happily
    fetch the wrong thing and ffmpeg will crash on a non-video file.
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
    # Allow exact matches and any *.tiktok.com subdomain.
    return netloc in _TIKTOK_HOSTS or netloc.endswith(".tiktok.com")


def normalize_tiktok_url(url: str) -> str:
    """Strip query strings, normalize host, return canonical form.

    Examples:
    - https://www.tiktok.com/@user/video/123?is_from_webapp=1
        -> https://www.tiktok.com/@user/video/123
    - https://m.tiktok.com/@user/photo/456
        -> https://www.tiktok.com/@user/photo/456
    - https://www.tiktok.com/@user/video/123'   (trailing quote from shell)
        -> https://www.tiktok.com/@user/video/123

    Non-TikTok URLs are returned unchanged so the caller can decide
    what to do with them. Use `is_tiktok_url()` to gate first.
    """
    if not url:
        return url
    # Strip shell/chat punctuation that snuck onto the end.
    url = _TRAILING_GARBAGE_RE.sub("", url.strip())
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    netloc = parsed.netloc.lower()
    if netloc.startswith("m.tiktok.com"):
        netloc = "www.tiktok.com"
    elif netloc == "tiktok.com":
        netloc = "www.tiktok.com"

    return urlunparse((
        parsed.scheme or "https",
        netloc,
        parsed.path,
        "",  # params
        "",  # query
        "",  # fragment
    ))


def extract_video_id(url: str) -> str | None:
    """Pull the numeric post id out of a TikTok URL, if present.

    Works for both /video/ and /photo/ URLs. The name "video_id" is kept
    for back-compat with existing schema columns; semantically it's the
    post id for either type.
    """
    if not url:
        return None
    m = _POST_PATH_RE.search(url)
    return m.group(3) if m else None


def extract_handle(url: str) -> str | None:
    """Pull the @handle (without the @) out of a TikTok URL, if present."""
    if not url:
        return None
    m = _POST_PATH_RE.search(url)
    return m.group(1) if m else None


def is_photo_post(url: str) -> bool:
    """Return True iff the URL is a /photo/ post."""
    if not url:
        return False
    m = _POST_PATH_RE.search(url)
    return bool(m and m.group(2) == "photo")


def post_type(url: str) -> str:
    """Return 'video', 'photo', or 'unknown' based on URL shape."""
    if not url:
        return "unknown"
    m = _POST_PATH_RE.search(url)
    if not m:
        return "unknown"
    return m.group(2)


def normalize_handle(raw: str) -> str:
    """Strip leading @ and whitespace from a handle."""
    return (raw or "").strip().lstrip("@").strip()


def creator_profile_url(handle: str) -> str:
    """Build the canonical profile URL for a handle."""
    return f"https://www.tiktok.com/@{normalize_handle(handle)}"
