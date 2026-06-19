"""Tests for media_archive.sources.tiktok.process.photo — JSON parsing, no network."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="tt-test-photo-"))
    monkeypatch.setenv("TT_DATA_DIR", str(tmp))
    monkeypatch.setenv("TT_DB_PATH", str(tmp / "test.db"))
    monkeypatch.setenv("TT_DB_URL", f"sqlite:///{tmp / 'test.db'}")
    monkeypatch.setenv("TT_LOG_LEVEL", "WARNING")
    import importlib
    import media_archive.core.config as cfg
    importlib.reload(cfg)
    yield tmp


# Sample shape of a TikTok photo post item, captured from a real
# rehydration blob and trimmed to the fields the parser cares about.
SAMPLE_ITEM = {
    "id": "7605019719636176135",
    "desc": "Cool slideshow caption",
    "createTime": 1709512000,
    "author": {
        "id": "12345",
        "uniqueId": "swerikcodes",
        "nickname": "Swerik Codes",
    },
    "imagePost": {
        "title": "",
        "images": [
            {
                "imageURL": {
                    "urlList": [
                        "https://p16-sign.tiktokcdn-us.com/slide-1.webp",
                        "https://p16-sign.tiktokcdn-us.com/slide-1-fallback.webp",
                    ]
                },
                "imageWidth": 1080,
                "imageHeight": 1080,
            },
            {
                "imageURL": {
                    "urlList": [
                        "https://p16-sign.tiktokcdn-us.com/slide-2.webp",
                    ]
                },
            },
            {
                "imageURL": {
                    "urlList": [
                        "https://p16-sign.tiktokcdn-us.com/slide-3.webp",
                    ]
                },
            },
        ],
    },
    "music": {
        "id": "999",
        "title": "Original sound",
        "playUrl": "https://sf16-ies-music.tiktokcdn-us.com/audio.mp3",
    },
}


SAMPLE_REHYDRATION = {
    "__DEFAULT_SCOPE__": {
        "webapp.video-detail": {
            "itemInfo": {"itemStruct": SAMPLE_ITEM}
        }
    }
}


def test_extract_image_urls_from_carousel():
    from media_archive.sources.tiktok.process.photo import _extract_image_urls
    urls = _extract_image_urls(SAMPLE_ITEM)
    assert urls == [
        "https://p16-sign.tiktokcdn-us.com/slide-1.webp",
        "https://p16-sign.tiktokcdn-us.com/slide-2.webp",
        "https://p16-sign.tiktokcdn-us.com/slide-3.webp",
    ]


def test_extract_image_urls_handles_missing_images():
    from media_archive.sources.tiktok.process.photo import _extract_image_urls
    assert _extract_image_urls({}) == []
    assert _extract_image_urls({"imagePost": {}}) == []
    assert _extract_image_urls({"imagePost": {"images": "not a list"}}) == []


def test_extract_audio_url_from_music():
    from media_archive.sources.tiktok.process.photo import _extract_audio_url
    assert _extract_audio_url(SAMPLE_ITEM) == "https://sf16-ies-music.tiktokcdn-us.com/audio.mp3"


def test_extract_audio_url_handles_missing():
    from media_archive.sources.tiktok.process.photo import _extract_audio_url
    assert _extract_audio_url({}) is None
    assert _extract_audio_url({"music": {}}) is None
    assert _extract_audio_url({"music": {"playUrl": ""}}) is None


def test_extract_audio_url_from_url_list_form():
    """Some posts have music.playUrl as a dict with urlList, not a string."""
    from media_archive.sources.tiktok.process.photo import _extract_audio_url
    item = {
        "music": {
            "playUrl": {
                "urlList": [
                    "",
                    "https://cdn.example/audio.mp3",
                ]
            }
        }
    }
    assert _extract_audio_url(item) == "https://cdn.example/audio.mp3"


def test_extract_metadata():
    from media_archive.sources.tiktok.process.photo import _extract_metadata
    meta = _extract_metadata(SAMPLE_ITEM)
    assert meta["description"] == "Cool slideshow caption"
    assert meta["author_handle"] == "swerikcodes"
    assert meta["author_display_name"] == "Swerik Codes"
    assert meta["upload_timestamp"] == 1709512000


def test_walk_to_item_finds_video_detail_path():
    from media_archive.sources.tiktok.process.photo import _walk_to_item
    item = _walk_to_item(SAMPLE_REHYDRATION)
    assert item is not None
    assert item["id"] == "7605019719636176135"


def test_walk_to_item_falls_back_to_alt_paths():
    """If TikTok rotates to the photo-detail path, we should still find it."""
    from media_archive.sources.tiktok.process.photo import _walk_to_item
    alt_data = {
        "__DEFAULT_SCOPE__": {
            "webapp.photo-detail": {"itemInfo": {"itemStruct": SAMPLE_ITEM}}
        }
    }
    item = _walk_to_item(alt_data)
    assert item is not None
    assert item["id"] == "7605019719636176135"


def test_walk_to_item_returns_none_for_unknown_shape():
    from media_archive.sources.tiktok.process.photo import _walk_to_item
    assert _walk_to_item({}) is None
    assert _walk_to_item({"__DEFAULT_SCOPE__": {"weird-key": {}}}) is None
    assert _walk_to_item("not a dict") is None


def test_extract_rehydration_json_finds_script_tag():
    from media_archive.sources.tiktok.process.photo import _extract_rehydration_json
    payload = json.dumps(SAMPLE_REHYDRATION)
    html = f"""<!DOCTYPE html>
<html><head><title>Photo</title></head><body>
<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{payload}</script>
</body></html>"""
    out = _extract_rehydration_json(html)
    assert out == SAMPLE_REHYDRATION


def test_extract_rehydration_json_returns_none_when_missing():
    from media_archive.sources.tiktok.process.photo import _extract_rehydration_json
    assert _extract_rehydration_json("<html>no script here</html>") is None


def test_extract_rehydration_json_handles_malformed_json():
    from media_archive.sources.tiktok.process.photo import _extract_rehydration_json
    html = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{not valid json</script>'
    assert _extract_rehydration_json(html) is None


def test_fetch_and_process_full_flow_with_mocked_http(monkeypatch):
    """End-to-end with HTTP and Whisper mocked. Verifies the processor wires
    image URLs, audio URL, and metadata into a PhotoPostResult correctly."""
    from media_archive.sources.tiktok.process import photo as photo_module

    payload = json.dumps(SAMPLE_REHYDRATION)
    fake_html = f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{payload}</script>'

    monkeypatch.setattr(photo_module, "_fetch_post_html", lambda url, timeout=30: fake_html)
    # Audio download succeeds; transcribe_audio_file returns a usable transcript
    monkeypatch.setattr(photo_module, "_download_to", lambda url, path, timeout=60: True)
    monkeypatch.setattr(
        photo_module,
        "_transcribe_audio_file",
        lambda audio_path: ("This is the audio transcript of a photo post slideshow.", "en"),
    )

    result = photo_module.fetch_and_process(
        "https://www.tiktok.com/@swerikcodes/photo/7605019719636176135"
    )

    assert result.success is True
    assert result.image_count == 3
    assert len(result.image_urls) == 3
    assert result.audio_url == "https://sf16-ies-music.tiktokcdn-us.com/audio.mp3"
    assert result.author_handle == "swerikcodes"
    assert result.transcript == "This is the audio transcript of a photo post slideshow."
    assert result.transcript_lang == "en"
    assert result.transcript_source == "audio"


def test_fetch_and_process_falls_back_to_empty_when_audio_silent(monkeypatch):
    """When audio transcribes to nothing meaningful and no tesseract, returns empty."""
    from media_archive.sources.tiktok.process import photo as photo_module

    payload = json.dumps(SAMPLE_REHYDRATION)
    fake_html = f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{payload}</script>'

    monkeypatch.setattr(photo_module, "_fetch_post_html", lambda url, timeout=30: fake_html)
    monkeypatch.setattr(photo_module, "_download_to", lambda url, path, timeout=60: True)
    monkeypatch.setattr(photo_module, "_transcribe_audio_file", lambda p: ("", ""))
    # Force tesseract to appear unavailable
    monkeypatch.setattr(photo_module, "tesseract_available", lambda: False)

    result = photo_module.fetch_and_process(
        "https://www.tiktok.com/@u/photo/123"
    )
    assert result.success is True  # still a success — we got the metadata
    assert result.transcript == ""
    assert result.transcript_source == "none"
    assert result.image_count == 3


def test_fetch_and_process_uses_ocr_when_audio_silent(monkeypatch, tmp_path):
    """When audio is silent and tesseract is available, OCR slides."""
    from media_archive.sources.tiktok.process import photo as photo_module

    payload = json.dumps(SAMPLE_REHYDRATION)
    fake_html = f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{payload}</script>'

    monkeypatch.setattr(photo_module, "_fetch_post_html", lambda url, timeout=30: fake_html)
    monkeypatch.setattr(photo_module, "_download_to", lambda url, path, timeout=60: True)
    monkeypatch.setattr(photo_module, "_transcribe_audio_file", lambda p: ("", ""))
    monkeypatch.setattr(photo_module, "tesseract_available", lambda: True)

    # Mock OCR to return distinct text per slide
    captured: list[str] = []
    def fake_ocr(image_path):
        captured.append(str(image_path))
        idx = len(captured)
        return f"text from slide {idx}"
    monkeypatch.setattr(photo_module, "_ocr_image", fake_ocr)

    result = photo_module.fetch_and_process(
        "https://www.tiktok.com/@u/photo/456"
    )
    assert result.success is True
    assert result.transcript_source == "ocr"
    assert "text from slide 1" in result.transcript
    assert "text from slide 3" in result.transcript
    assert "[Slide 1]" in result.transcript
    assert len(captured) == 3


def test_fetch_and_process_handles_404_html(monkeypatch):
    """If the page has no rehydration script, return failure."""
    from media_archive.sources.tiktok.process import photo as photo_module

    monkeypatch.setattr(
        photo_module, "_fetch_post_html",
        lambda url, timeout=30: "<html>not found</html>",
    )

    result = photo_module.fetch_and_process(
        "https://www.tiktok.com/@u/photo/dead"
    )
    assert result.success is False
    assert "rehydration" in (result.error or "").lower()


def test_fetch_and_process_handles_network_error(monkeypatch):
    """If both the HTTP fetch and browser fallback fail, return failure.

    Behavior changed in v0.2.5: a plain-fetch failure now triggers the
    Playwright browser fallback. We need to mock that too, and the final
    error should reflect whichever fallback failed last.
    """
    import requests as req
    from media_archive.sources.tiktok.process import photo as photo_module
    from media_archive.sources.tiktok.process import browser_fetch

    def boom(url, timeout=30):
        raise req.ConnectionError("DNS failed")
    monkeypatch.setattr(photo_module, "_fetch_post_html", boom)

    def browser_boom(url, **kwargs):
        raise browser_fetch.BrowserFetchUnavailable("playwright not installed")
    monkeypatch.setattr(browser_fetch, "fetch_with_browser", browser_boom)

    result = photo_module.fetch_and_process(
        "https://www.tiktok.com/@u/photo/789"
    )
    assert result.success is False
    # When plain fetch fails AND browser is unavailable, we expect the
    # browser-unavailable message — that's the actionable info for the user.
    assert "Playwright" in (result.error or "") or "playwright" in (result.error or "")


def test_fetch_and_process_handles_browser_error(monkeypatch):
    """If plain fetch fails and browser is installed but errors out,
    surface the browser error so the user knows what went wrong."""
    import requests as req
    from media_archive.sources.tiktok.process import photo as photo_module
    from media_archive.sources.tiktok.process import browser_fetch

    def boom(url, timeout=30):
        raise req.ConnectionError("DNS failed")
    monkeypatch.setattr(photo_module, "_fetch_post_html", boom)

    def browser_err(url, **kwargs):
        raise browser_fetch.BrowserFetchError("captcha challenge")
    monkeypatch.setattr(browser_fetch, "fetch_with_browser", browser_err)

    result = photo_module.fetch_and_process(
        "https://www.tiktok.com/@u/photo/789"
    )
    assert result.success is False
    assert "captcha" in (result.error or "").lower()


def test_combined_audio_plus_ocr_when_audio_too_short(monkeypatch):
    """If audio is slightly under the threshold but OCR succeeds, combine them."""
    from media_archive.sources.tiktok.process import photo as photo_module

    payload = json.dumps(SAMPLE_REHYDRATION)
    fake_html = f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{payload}</script>'

    monkeypatch.setattr(photo_module, "_fetch_post_html", lambda url, timeout=30: fake_html)
    monkeypatch.setattr(photo_module, "_download_to", lambda url, path, timeout=60: True)
    # 10 chars < 20-char threshold
    monkeypatch.setattr(photo_module, "_transcribe_audio_file", lambda p: ("um yeah ok", "en"))
    monkeypatch.setattr(photo_module, "tesseract_available", lambda: True)
    monkeypatch.setattr(photo_module, "_ocr_image", lambda p: "Real text on the slide")

    result = photo_module.fetch_and_process(
        "https://www.tiktok.com/@u/photo/777"
    )
    assert result.success is True
    assert result.transcript_source == "audio+ocr"
    assert "um yeah ok" in result.transcript
    assert "Real text on the slide" in result.transcript
    assert "Slide text (OCR)" in result.transcript


def test_no_manual_accept_encoding_header():
    """We deliberately omit Accept-Encoding so requests handles decompression
    automatically. Setting it manually (especially 'br') causes mojibake."""
    from media_archive.sources.tiktok.process.photo import _HEADERS
    assert "Accept-Encoding" not in _HEADERS


def test_extract_rehydration_finds_sigi_state():
    """Fallback: TikTok used to use SIGI_STATE before UNIVERSAL_DATA."""
    import json as _json
    from media_archive.sources.tiktok.process.photo import _extract_rehydration_json
    payload = _json.dumps({"ItemModule": {"123": {"id": "123", "desc": "old format"}}})
    html = f'<script id="SIGI_STATE">{payload}</script>'
    out = _extract_rehydration_json(html)
    assert out is not None
    assert "ItemModule" in out


def test_extract_rehydration_finds_next_data():
    """Fallback: TikTok could also use Next.js __NEXT_DATA__ shape."""
    import json as _json
    from media_archive.sources.tiktok.process.photo import _extract_rehydration_json
    payload = _json.dumps({"props": {"pageProps": {"itemInfo": {"itemStruct": {"id": "456"}}}}})
    html = f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'
    out = _extract_rehydration_json(html)
    assert out is not None
    assert out.get("props", {}).get("pageProps", {}).get("itemInfo", {}).get("itemStruct", {}).get("id") == "456"


def test_walk_to_item_handles_next_data_shape():
    """Verify _walk_to_item finds posts in Next.js shape."""
    from media_archive.sources.tiktok.process.photo import _walk_to_item
    data = {
        "props": {
            "pageProps": {
                "itemInfo": {
                    "itemStruct": {
                        "id": "789",
                        "desc": "from next data",
                        "imagePost": {"images": []},
                    }
                }
            }
        }
    }
    item = _walk_to_item(data)
    assert item is not None
    assert item["id"] == "789"


def test_walk_to_item_handles_sigi_state_shape():
    """Verify _walk_to_item finds posts in SIGI_STATE shape."""
    from media_archive.sources.tiktok.process.photo import _walk_to_item
    data = {
        "ItemModule": {
            "9999": {"id": "9999", "desc": "sigi format", "imagePost": {"images": []}}
        }
    }
    item = _walk_to_item(data)
    assert item is not None
    assert item["id"] == "9999"
