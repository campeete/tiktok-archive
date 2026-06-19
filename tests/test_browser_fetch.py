"""Tests for the Playwright-based browser fetcher.

We can't launch a real Chromium in CI, so these tests focus on:
- Graceful handling when Playwright isn't installed
- Correct exception types
- The is_browser_available() smoke test
- Integration with photo.py: fallback path is taken when plain fetch
  returns a captcha page

The actual end-to-end Playwright call is exercised manually via the
debug-photo CLI on Cameron's Mac.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch


def test_browser_fetch_unavailable_when_playwright_missing(monkeypatch):
    """If playwright is not installed, we get BrowserFetchUnavailable.

    We simulate this by removing 'playwright' from sys.modules and
    making the import fail.
    """
    from media_archive.sources.tiktok.process import browser_fetch

    # Simulate ImportError on `from playwright.sync_api import ...`
    with patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
        try:
            browser_fetch.fetch_with_browser("https://example.com")
        except browser_fetch.BrowserFetchUnavailable as e:
            assert "Playwright" in str(e) or "playwright" in str(e)
            return
    raise AssertionError("Expected BrowserFetchUnavailable")


def test_is_browser_available_returns_false_without_playwright():
    """The smoke check should return False when playwright is missing."""
    from media_archive.sources.tiktok.process import browser_fetch

    with patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
        assert browser_fetch.is_browser_available() is False


def test_browser_fetch_error_distinct_from_unavailable():
    """BrowserFetchError and BrowserFetchUnavailable are different types.

    BrowserFetchUnavailable means "install Playwright to use this".
    BrowserFetchError means "Playwright is installed, but the fetch failed".
    The distinction matters because the photo.py caller surfaces different
    error messages to the user.
    """
    from media_archive.sources.tiktok.process import browser_fetch

    assert browser_fetch.BrowserFetchError is not browser_fetch.BrowserFetchUnavailable
    assert not issubclass(
        browser_fetch.BrowserFetchError,
        browser_fetch.BrowserFetchUnavailable,
    )
    assert not issubclass(
        browser_fetch.BrowserFetchUnavailable,
        browser_fetch.BrowserFetchError,
    )


def test_photo_falls_back_to_browser_when_plain_fetch_returns_shell(monkeypatch):
    """The full integration: plain fetch returns the captcha shell, photo.py
    must call into browser_fetch.fetch_with_browser as a fallback.

    We use a post with no images/audio so we exit before the heavy stuff
    (Whisper, OCR, network). The point of the test is just confirming the
    fallback fired and the item was extracted from the browser HTML.
    """
    from media_archive.sources.tiktok.process import photo as photo_module
    from media_archive.sources.tiktok.process import browser_fetch

    # Captcha shell: rehydration JSON present but only app-shell scopes.
    shell_html = (
        '<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        '{"__DEFAULT_SCOPE__":{"webapp.app-context":{},"webapp.i18n-translation":{}}}'
        '</script></html>'
    )
    # Browser-rendered "real" page: has the post item but no images/audio,
    # which makes the function return early at the "no urls" check.
    real_html = (
        '<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        '{"__DEFAULT_SCOPE__":{"webapp.video-detail":{"itemInfo":{"itemStruct":'
        '{"id":"7605019719636176135","desc":"a test photo post",'
        '"author":{"uniqueId":"swerikcodes"},"createTime":1700000000}'
        '}}}}</script></html>'
    )

    monkeypatch.setattr(photo_module, "_fetch_post_html", lambda url, timeout=30: shell_html)

    browser_called = {"n": 0}

    def fake_browser_fetch(url, **kwargs):
        browser_called["n"] += 1
        return real_html

    monkeypatch.setattr(browser_fetch, "fetch_with_browser", fake_browser_fetch)

    result = photo_module.fetch_and_process(
        "https://www.tiktok.com/@swerikcodes/photo/7605019719636176135"
    )

    # Browser fallback ran exactly once
    assert browser_called["n"] == 1
    # The item parsed from the browser-rendered HTML
    assert "No image URLs or audio URL" in (result.error or "")


def test_photo_skips_browser_when_plain_fetch_already_has_data(monkeypatch):
    """If plain fetch returns real post data, photo.py must NOT invoke the
    browser fetcher — it would just be slower for no reason.
    """
    from media_archive.sources.tiktok.process import photo as photo_module
    from media_archive.sources.tiktok.process import browser_fetch

    real_html = (
        '<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        '{"__DEFAULT_SCOPE__":{"webapp.video-detail":{"itemInfo":{"itemStruct":'
        '{"id":"123","desc":"works on plain fetch","author":{"uniqueId":"u"}'
        '}}}}}</script></html>'
    )

    monkeypatch.setattr(photo_module, "_fetch_post_html", lambda url, timeout=30: real_html)

    browser_called = {"n": 0}

    def fake_browser_fetch(url, **kwargs):
        browser_called["n"] += 1
        return real_html

    monkeypatch.setattr(browser_fetch, "fetch_with_browser", fake_browser_fetch)

    photo_module.fetch_and_process("https://www.tiktok.com/@u/photo/123")

    assert browser_called["n"] == 0, (
        "Browser fetcher must not be invoked when plain fetch returns real data"
    )


def test_have_saved_profile_returns_false_when_dir_missing(monkeypatch, tmp_path):
    """If the profile dir doesn't exist, _have_saved_profile is False."""
    from media_archive.sources.tiktok.process import browser_fetch
    from media_archive.core import config
    monkeypatch.setattr(config, "BROWSER_PROFILE_DIR", tmp_path / "no-such-dir")
    assert browser_fetch._have_saved_profile() is False


def test_have_saved_profile_returns_false_when_no_cookies_file(monkeypatch, tmp_path):
    """An empty profile dir (no Default/Cookies file) means no saved auth.

    Playwright creates an empty user_data_dir on first run even if the
    user closes the browser without logging in. We need a stronger signal
    than mere directory presence.
    """
    from media_archive.sources.tiktok.process import browser_fetch
    from media_archive.core import config
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    monkeypatch.setattr(config, "BROWSER_PROFILE_DIR", profile_dir)
    assert browser_fetch._have_saved_profile() is False


def test_have_saved_profile_returns_true_when_cookies_present(monkeypatch, tmp_path):
    """A Default/Cookies file with content means a real session exists."""
    from media_archive.sources.tiktok.process import browser_fetch
    from media_archive.core import config
    profile_dir = tmp_path / "profile"
    (profile_dir / "Default").mkdir(parents=True)
    cookies = profile_dir / "Default" / "Cookies"
    cookies.write_bytes(b"SQLite format 3\x00fake-but-non-empty")
    monkeypatch.setattr(config, "BROWSER_PROFILE_DIR", profile_dir)
    assert browser_fetch._have_saved_profile() is True


def test_have_saved_profile_returns_false_when_cookies_empty(monkeypatch, tmp_path):
    """An empty Cookies file is treated the same as no cookies — Chromium
    sometimes creates the file before any sessions exist."""
    from media_archive.sources.tiktok.process import browser_fetch
    from media_archive.core import config
    profile_dir = tmp_path / "profile"
    (profile_dir / "Default").mkdir(parents=True)
    (profile_dir / "Default" / "Cookies").write_bytes(b"")
    monkeypatch.setattr(config, "BROWSER_PROFILE_DIR", profile_dir)
    assert browser_fetch._have_saved_profile() is False
