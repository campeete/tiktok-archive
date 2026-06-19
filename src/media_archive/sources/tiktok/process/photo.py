"""
Photo post processor.

TikTok photo posts (URLs of the form /@user/photo/<id>) are slideshows: 1-35
still images plus an optional audio track. yt-dlp does NOT support these —
the TikTok extractor returns 'Unsupported URL'. We work around it by:

1. Fetching the photo post page as a regular browser would.
2. Parsing the embedded __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON blob,
   which contains direct CDN URLs for the slide images and the audio.
3. Downloading the audio (if present) and running Whisper on it.
4. If the audio yields nothing useful AND tesseract is installed,
   downloading each slide image and running OCR to capture text overlays.
5. Writing a unified transcript artifact like a video would produce.

The slide images themselves are NOT kept on disk — they're downloaded into
a tempdir, OCR'd, and discarded. Same transcribe-and-discard policy as videos.

If TikTok's JSON structure changes (which they rotate every few weeks),
the parser falls back gracefully: missing images = empty transcript = stub
summary, exactly like the existing empty-transcript path in analyze.py.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from media_archive.core import config
from media_archive.core.transcribe.transcribe import transcribe_video_file

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class PhotoPostResult:
    """Outcome of trying to process a photo post."""
    success: bool
    transcript: str = ""
    transcript_lang: str = ""
    transcript_source: str = ""  # "audio" | "ocr" | "audio+ocr" | "none"
    image_count: int = 0
    image_urls: list[str] = field(default_factory=list)
    audio_url: str | None = None
    description: str | None = None
    author_handle: str | None = None
    author_display_name: str | None = None
    upload_timestamp: int | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# HTML fetcher
# ---------------------------------------------------------------------------

# Browser-like headers. TikTok's anti-bot won't return rehydration JSON to
# obvious bot user agents.
#
# Note: deliberately omitting Accept-Encoding so `requests` negotiates encoding
# itself and decompresses the response transparently. If we set Accept-Encoding
# manually (especially "br"), requests will return the raw compressed bytes
# unless we have the right decoder installed, which produces mojibake.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# TikTok periodically rotates the name of the rehydration script tag.
# We try each in order and use the first one we find.
_SCRIPT_RES = [
    re.compile(
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.+?)</script>',
        re.DOTALL,
    ),
    re.compile(
        r'<script[^>]+id="SIGI_STATE"[^>]*>(.+?)</script>',
        re.DOTALL,
    ),
    re.compile(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.+?)</script>',
        re.DOTALL,
    ),
]
# Back-compat alias
_SCRIPT_RE = _SCRIPT_RES[0]


def _fetch_post_html(url: str, timeout: int = 30) -> str:
    """GET the photo post URL as a browser would. Raises on non-2xx.

    Uses requests' automatic content-encoding handling — do not set
    Accept-Encoding manually or the response body comes back compressed.
    """
    resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    # resp.text uses the charset from Content-Type, falling back to ISO-8859-1
    # when none is specified. TikTok serves UTF-8 but doesn't always set the
    # charset header, so we force UTF-8 to avoid mojibake on the JSON blob.
    resp.encoding = resp.encoding if resp.encoding and resp.encoding.lower() != "iso-8859-1" else "utf-8"
    return resp.text


def _extract_rehydration_json(html: str) -> dict | None:
    """Pull the rehydration JSON out of the page HTML.

    Tries multiple known script-tag IDs since TikTok periodically rotates
    them. Returns the parsed dict, or None if no known tag is present.
    """
    for pattern in _SCRIPT_RES:
        m = pattern.search(html)
        if not m:
            continue
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as e:
            logger.warning("Could not parse JSON from %s: %s", pattern.pattern[:50], e)
            continue
    return None


def _walk_to_item(data: dict) -> dict | None:
    """Walk into the rehydration blob to find the post data.

    TikTok rotates the exact key path every few weeks, and the top-level
    structure differs between rehydration script types. We try the known
    locations in order. If they all miss, we return None and the caller
    falls through to the empty-transcript stub.
    """
    if not isinstance(data, dict):
        return None

    # __UNIVERSAL_DATA_FOR_REHYDRATION__ shape
    scope = data.get("__DEFAULT_SCOPE__", {})
    if isinstance(scope, dict):
        candidates = [
            ("webapp.video-detail", "itemInfo", "itemStruct"),
            ("webapp.photo-detail", "itemInfo", "itemStruct"),
            ("webapp.item-detail", "itemInfo", "itemStruct"),
        ]
        for path in candidates:
            cur: Any = scope
            for key in path:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(key)
                if cur is None:
                    break
            if isinstance(cur, dict) and cur.get("id"):
                return cur

    # __NEXT_DATA__ shape (Next.js): props.pageProps.itemInfo.itemStruct
    props = data.get("props", {})
    if isinstance(props, dict):
        cur = props.get("pageProps", {})
        if isinstance(cur, dict):
            info = cur.get("itemInfo")
            if isinstance(info, dict):
                struct = info.get("itemStruct")
                if isinstance(struct, dict) and struct.get("id"):
                    return struct

    # SIGI_STATE shape: ItemModule[<id>]
    item_module = data.get("ItemModule")
    if isinstance(item_module, dict):
        for v in item_module.values():
            if isinstance(v, dict) and v.get("id"):
                return v

    return None


# ---------------------------------------------------------------------------
# Parsing the item dict
# ---------------------------------------------------------------------------

def _extract_image_urls(item: dict) -> list[str]:
    """Pull the slide image URLs out of an item dict.

    Photo posts have:
      item['imagePost']['images'][n]['imageURL']['urlList']  -- list of CDN URLs,
        first being the highest priority

    Returns a list of unique URLs in carousel order.
    """
    image_post = item.get("imagePost")
    if not isinstance(image_post, dict):
        return []
    images = image_post.get("images")
    if not isinstance(images, list):
        return []

    urls: list[str] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        url_obj = img.get("imageURL") or img.get("imageUrl") or {}
        url_list = url_obj.get("urlList") if isinstance(url_obj, dict) else None
        if isinstance(url_list, list) and url_list:
            for u in url_list:
                if isinstance(u, str) and u.startswith("http"):
                    urls.append(u)
                    break  # take the first valid CDN URL per slide
    return urls


def _extract_audio_url(item: dict) -> str | None:
    """Pull the audio track URL from a photo post item.

    'music.playUrl' is the canonical location. Some posts have multiple
    audio sources; we take the first.
    """
    music = item.get("music")
    if not isinstance(music, dict):
        return None
    play_url = music.get("playUrl")
    if isinstance(play_url, str) and play_url.startswith("http"):
        return play_url
    if isinstance(play_url, dict):
        for u in play_url.get("urlList") or []:
            if isinstance(u, str) and u.startswith("http"):
                return u
    return None


def _extract_metadata(item: dict) -> dict:
    """Pull description, author, timestamps from an item dict."""
    author = item.get("author") or {}
    return {
        "description": item.get("desc"),
        "author_handle": author.get("uniqueId"),
        "author_display_name": author.get("nickname"),
        "upload_timestamp": item.get("createTime"),
    }


# ---------------------------------------------------------------------------
# OCR support (tesseract optional)
# ---------------------------------------------------------------------------

def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _ocr_image(image_path: Path) -> str:
    """Run tesseract on one image, return extracted text."""
    if not tesseract_available():
        return ""
    cmd = ["tesseract", str(image_path), "-", "-l", "eng", "--psm", "6"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("tesseract failed on %s: %s", image_path, e)
    return ""


# ---------------------------------------------------------------------------
# Audio download + transcription helpers
# ---------------------------------------------------------------------------

def _download_to(url: str, path: Path, timeout: int = 60) -> bool:
    """Stream a URL to a file. Returns True on success."""
    try:
        with requests.get(url, headers=_HEADERS, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with path.open("wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
        return path.stat().st_size > 0
    except (requests.RequestException, OSError) as e:
        logger.warning("Download failed %s -> %s: %s", url, path, e)
        return False


def _transcribe_audio_file(audio_path: Path) -> tuple[str, str]:
    """Wrap Whisper to transcribe an audio file.

    transcribe_video_file accepts any media file ffmpeg can read, so an
    .mp3 or .m4a works fine — ffmpeg extracts the audio stream regardless
    of container.
    """
    return transcribe_video_file(audio_path)


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------

def fetch_and_process(
    url: str,
    *,
    enable_ocr: bool = True,
    min_audio_chars: int = 20,
) -> PhotoPostResult:
    """Process one TikTok photo post URL.

    Returns a PhotoPostResult. On failure, success=False with an error message.
    Never raises — all errors are reported via the result.
    """
    logger.info("Processing photo post: %s", url)

    # 1) Fetch the page. We try plain requests first (fast). If the page
    # comes back without recognizable post data — the smoking gun for
    # TikTok serving us the anti-bot landing page — we retry with a
    # real headless browser, which executes JS and clears the challenge.
    html: str | None = None
    fetch_method = "requests"
    try:
        html = _fetch_post_html(url)
    except requests.RequestException as e:
        logger.warning("Plain fetch failed (%s); will try browser fetch", e)
        html = None

    data = _extract_rehydration_json(html) if html else None
    item = _walk_to_item(data) if data else None

    if item is None:
        # Plain fetch didn't return real post data. This is the typical
        # case: TikTok served us the captcha shell. Retry with Playwright.
        logger.info("Plain fetch missed post data; falling back to browser")
        from media_archive.sources.tiktok.process import browser_fetch
        try:
            html = browser_fetch.fetch_with_browser(url)
            fetch_method = "browser"
        except browser_fetch.BrowserFetchUnavailable as e:
            return PhotoPostResult(
                success=False,
                error=(
                    f"Plain fetch returned anti-bot page and Playwright is "
                    f"unavailable: {e}"
                ),
            )
        except browser_fetch.BrowserFetchError as e:
            return PhotoPostResult(
                success=False,
                error=f"Browser fetch failed: {e}",
            )

        # 2) Re-extract from the browser-rendered HTML
        data = _extract_rehydration_json(html)
        if data is None:
            return PhotoPostResult(
                success=False,
                error="Could not find __UNIVERSAL_DATA_FOR_REHYDRATION__ in browser-rendered page",
            )

        item = _walk_to_item(data)
        if item is None:
            return PhotoPostResult(
                success=False,
                error="Could not locate item data in rehydration JSON (TikTok may have rotated keys)",
            )

    logger.debug("Got post data via %s fetch", fetch_method)

    # 3) Extract URLs and metadata
    image_urls = _extract_image_urls(item)
    audio_url = _extract_audio_url(item)
    meta = _extract_metadata(item)

    if not image_urls and not audio_url:
        return PhotoPostResult(
            success=False,
            error="No image URLs or audio URL found (post may be private or deleted)",
        )

    result = PhotoPostResult(
        success=True,
        image_count=len(image_urls),
        image_urls=image_urls,
        audio_url=audio_url,
        description=meta.get("description"),
        author_handle=meta.get("author_handle"),
        author_display_name=meta.get("author_display_name"),
        upload_timestamp=meta.get("upload_timestamp"),
    )

    # 4) Process audio + OCR in a tempdir; everything cleaned up on exit
    with tempfile.TemporaryDirectory(prefix="tt-photo-") as tmpdir:
        tmp = Path(tmpdir)

        audio_text = ""
        audio_lang = ""
        if audio_url:
            audio_path = tmp / "audio.mp3"
            if _download_to(audio_url, audio_path):
                try:
                    audio_text, audio_lang = _transcribe_audio_file(audio_path)
                    logger.info(
                        "Photo post audio transcribed (%d chars)", len(audio_text or "")
                    )
                except Exception as e:
                    logger.warning("Photo post audio transcription failed: %s", e)
            else:
                logger.warning("Could not download photo post audio")

        # If audio gave us a meaningful transcript, that's good enough.
        if len((audio_text or "").strip()) >= min_audio_chars:
            result.transcript = audio_text.strip()
            result.transcript_lang = audio_lang or "en"
            result.transcript_source = "audio"
            return result

        # Otherwise try OCR on the slide images, if the user has tesseract.
        if enable_ocr and image_urls and tesseract_available():
            ocr_chunks: list[str] = []
            for i, img_url in enumerate(image_urls, 1):
                img_path = tmp / f"slide-{i:02d}.jpg"
                if not _download_to(img_url, img_path):
                    continue
                txt = _ocr_image(img_path).strip()
                if txt:
                    ocr_chunks.append(f"[Slide {i}] {txt}")
            ocr_text = "\n\n".join(ocr_chunks).strip()
            if ocr_text:
                # Combine audio + OCR if both present
                if audio_text and audio_text.strip():
                    result.transcript = (
                        f"{audio_text.strip()}\n\n--- Slide text (OCR) ---\n\n{ocr_text}"
                    )
                    result.transcript_lang = audio_lang or "en"
                    result.transcript_source = "audio+ocr"
                else:
                    result.transcript = ocr_text
                    result.transcript_lang = "en"
                    result.transcript_source = "ocr"
                logger.info(
                    "Photo post OCR captured %d slide(s) of text", len(ocr_chunks)
                )
                return result
        elif enable_ocr and image_urls and not tesseract_available():
            logger.info(
                "Tesseract not installed; skipping OCR for photo post %s. "
                "Install with: brew install tesseract", url,
            )

        # Whatever we got — possibly empty, possibly a short audio fragment
        result.transcript = (audio_text or "").strip()
        result.transcript_lang = audio_lang or ""
        result.transcript_source = "audio" if audio_text else "none"
        return result
