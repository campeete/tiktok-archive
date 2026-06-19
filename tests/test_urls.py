"""Tests for media_archive.sources.tiktok.ingest.urls — pure functions."""
import pytest


def test_normalize_strips_query_string():
    from media_archive.sources.tiktok.ingest.urls import normalize_tiktok_url
    assert (
        normalize_tiktok_url("https://www.tiktok.com/@user/video/123?is_from_webapp=1&sender_device=pc")
        == "https://www.tiktok.com/@user/video/123"
    )


def test_normalize_replaces_mobile_host():
    from media_archive.sources.tiktok.ingest.urls import normalize_tiktok_url
    assert (
        normalize_tiktok_url("https://m.tiktok.com/@user/video/123")
        == "https://www.tiktok.com/@user/video/123"
    )


def test_normalize_handles_naked_host():
    from media_archive.sources.tiktok.ingest.urls import normalize_tiktok_url
    assert (
        normalize_tiktok_url("https://tiktok.com/@user/video/123")
        == "https://www.tiktok.com/@user/video/123"
    )


def test_normalize_idempotent():
    from media_archive.sources.tiktok.ingest.urls import normalize_tiktok_url
    canon = "https://www.tiktok.com/@user/video/123"
    assert normalize_tiktok_url(canon) == canon
    assert normalize_tiktok_url(normalize_tiktok_url(canon)) == canon


def test_normalize_empty_input():
    from media_archive.sources.tiktok.ingest.urls import normalize_tiktok_url
    assert normalize_tiktok_url("") == ""


def test_extract_video_id():
    from media_archive.sources.tiktok.ingest.urls import extract_video_id
    assert extract_video_id("https://www.tiktok.com/@user/video/7627389765796564244") == "7627389765796564244"
    assert extract_video_id("https://example.com/foo") is None
    assert extract_video_id("") is None


def test_extract_handle():
    from media_archive.sources.tiktok.ingest.urls import extract_handle
    assert extract_handle("https://www.tiktok.com/@sherenayaps/video/123") == "sherenayaps"
    assert extract_handle("https://example.com/foo") is None


def test_normalize_handle_strips_at_and_whitespace():
    from media_archive.sources.tiktok.ingest.urls import normalize_handle
    assert normalize_handle("@user") == "user"
    assert normalize_handle("  user  ") == "user"
    assert normalize_handle("@@user") == "user"
    assert normalize_handle("") == ""


def test_creator_profile_url():
    from media_archive.sources.tiktok.ingest.urls import creator_profile_url
    assert creator_profile_url("user") == "https://www.tiktok.com/@user"
    assert creator_profile_url("@user") == "https://www.tiktok.com/@user"


def test_extract_video_id_from_photo_url():
    from media_archive.sources.tiktok.ingest.urls import extract_video_id
    assert extract_video_id("https://www.tiktok.com/@user/photo/7605019719636176135") == "7605019719636176135"


def test_extract_handle_from_photo_url():
    from media_archive.sources.tiktok.ingest.urls import extract_handle
    assert extract_handle("https://www.tiktok.com/@swerikcodes/photo/7605019719636176135") == "swerikcodes"


def test_is_photo_post():
    from media_archive.sources.tiktok.ingest.urls import is_photo_post
    assert is_photo_post("https://www.tiktok.com/@u/photo/123") is True
    assert is_photo_post("https://www.tiktok.com/@u/video/123") is False
    assert is_photo_post("https://example.com/foo") is False
    assert is_photo_post("") is False


def test_post_type():
    from media_archive.sources.tiktok.ingest.urls import post_type
    assert post_type("https://www.tiktok.com/@u/video/123") == "video"
    assert post_type("https://www.tiktok.com/@u/photo/456") == "photo"
    assert post_type("https://example.com/foo") == "unknown"


def test_normalize_strips_query_from_photo_url():
    from media_archive.sources.tiktok.ingest.urls import normalize_tiktok_url
    raw = "https://www.tiktok.com/@user/photo/123?is_from_webapp=1&sender_device=pc"
    assert normalize_tiktok_url(raw) == "https://www.tiktok.com/@user/photo/123"


def test_handle_with_dot_in_it():
    """TikTok handles can contain dots, like @productiveronny.mp4"""
    from media_archive.sources.tiktok.ingest.urls import extract_handle, extract_video_id
    url = "https://www.tiktok.com/@productiveronny.mp4/video/7634812078901038366"
    assert extract_handle(url) == "productiveronny.mp4"
    assert extract_video_id(url) == "7634812078901038366"


# ---------- is_tiktok_url ---------- (Phase 1.7.2)

def test_is_tiktok_url_accepts_canonical():
    from media_archive.sources.tiktok.ingest.urls import is_tiktok_url
    assert is_tiktok_url("https://www.tiktok.com/@user/video/123") is True


def test_is_tiktok_url_accepts_mobile_host():
    from media_archive.sources.tiktok.ingest.urls import is_tiktok_url
    assert is_tiktok_url("https://m.tiktok.com/@user/video/123") is True


def test_is_tiktok_url_accepts_naked_host():
    from media_archive.sources.tiktok.ingest.urls import is_tiktok_url
    assert is_tiktok_url("https://tiktok.com/@user/video/123") is True


def test_is_tiktok_url_accepts_short_form_vm():
    from media_archive.sources.tiktok.ingest.urls import is_tiktok_url
    # vm.tiktok.com is the short-link domain TikTok hands out from
    # the share menu. yt-dlp follows the redirect to the canonical URL.
    assert is_tiktok_url("https://vm.tiktok.com/AbCdEf/") is True


def test_is_tiktok_url_rejects_youtube():
    from media_archive.sources.tiktok.ingest.urls import is_tiktok_url
    assert is_tiktok_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is False


def test_is_tiktok_url_rejects_instagram():
    from media_archive.sources.tiktok.ingest.urls import is_tiktok_url
    assert is_tiktok_url("https://www.instagram.com/reel/abc/") is False


def test_is_tiktok_url_rejects_garbage():
    from media_archive.sources.tiktok.ingest.urls import is_tiktok_url
    assert is_tiktok_url("this is not a url") is False
    assert is_tiktok_url("") is False
    assert is_tiktok_url("ftp://tiktok.com/x") is False


def test_is_tiktok_url_strips_trailing_quote():
    """zsh's dquote> mode often leaves a trailing single-quote when a
    paste spans lines. We strip it before classifying."""
    from media_archive.sources.tiktok.ingest.urls import is_tiktok_url
    assert is_tiktok_url("https://www.tiktok.com/@user/video/123'") is True
    assert is_tiktok_url('https://www.tiktok.com/@user/video/123"') is True


# ---------- normalize_tiktok_url trailing-garbage ----------

def test_normalize_strips_trailing_quote():
    """A trailing ' from a broken paste shouldn't poison the URL."""
    from media_archive.sources.tiktok.ingest.urls import normalize_tiktok_url
    assert (
        normalize_tiktok_url("https://www.tiktok.com/@user/video/123?x=1'")
        == "https://www.tiktok.com/@user/video/123"
    )


def test_normalize_strips_trailing_paren():
    """Markdown link paste sometimes leaves a closing paren."""
    from media_archive.sources.tiktok.ingest.urls import normalize_tiktok_url
    assert (
        normalize_tiktok_url("https://www.tiktok.com/@user/video/123)")
        == "https://www.tiktok.com/@user/video/123"
    )


def test_normalize_strips_trailing_period():
    """Sentence-end punctuation: 'I saw https://tiktok.com/x.'"""
    from media_archive.sources.tiktok.ingest.urls import normalize_tiktok_url
    assert (
        normalize_tiktok_url("https://www.tiktok.com/@user/video/123.")
        == "https://www.tiktok.com/@user/video/123"
    )
