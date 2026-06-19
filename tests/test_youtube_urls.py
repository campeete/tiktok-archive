"""Tests for media_archive.sources.youtube.ingest.urls (v0.2.0)."""
from media_archive.sources.youtube.ingest.urls import (
    extract_video_id,
    is_playlist_url,
    is_youtube_url,
    normalize_youtube_url,
)


# ---------------------------------------------------------------------------
# is_youtube_url
# ---------------------------------------------------------------------------

class TestIsYoutubeUrl:
    def test_canonical(self):
        assert is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_short_link(self):
        assert is_youtube_url("https://youtu.be/dQw4w9WgXcQ")

    def test_mobile(self):
        assert is_youtube_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_music(self):
        assert is_youtube_url("https://music.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_shorts(self):
        assert is_youtube_url("https://www.youtube.com/shorts/abcdefghijk")

    def test_subdomain(self):
        # Random *.youtube.com subdomain — accept defensively, yt-dlp will
        # tell us if the URL is actually fetchable.
        assert is_youtube_url("https://kids.youtube.com/watch?v=abcdefghijk")

    def test_naked_domain(self):
        assert is_youtube_url("https://youtube.com/watch?v=abcdefghijk")

    def test_rejects_tiktok(self):
        assert not is_youtube_url("https://www.tiktok.com/@x/video/123")

    def test_rejects_random(self):
        assert not is_youtube_url("https://example.com/foo")

    def test_rejects_blank(self):
        assert not is_youtube_url("")
        assert not is_youtube_url("   ")

    def test_rejects_non_http(self):
        # File paths, things without a scheme, etc.
        assert not is_youtube_url("youtube.com/watch?v=X")
        assert not is_youtube_url("/path/to/video.mp4")
        assert not is_youtube_url("ftp://youtube.com/")

    def test_handles_trailing_garbage(self):
        # zsh dquote artifacts, sentence punctuation, parens.
        assert is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ'")
        assert is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ.")
        assert is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ)")


# ---------------------------------------------------------------------------
# extract_video_id
# ---------------------------------------------------------------------------

class TestExtractVideoId:
    def test_canonical_watch(self):
        assert extract_video_id(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        ) == "dQw4w9WgXcQ"

    def test_short_link(self):
        assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_link_with_query(self):
        # ?si=... is YouTube's tracking ID — the video ID still wins
        assert extract_video_id(
            "https://youtu.be/dQw4w9WgXcQ?si=abc123def"
        ) == "dQw4w9WgXcQ"

    def test_mobile_watch(self):
        assert extract_video_id(
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ"
        ) == "dQw4w9WgXcQ"

    def test_shorts_url(self):
        assert extract_video_id(
            "https://www.youtube.com/shorts/dQw4w9WgXcQ"
        ) == "dQw4w9WgXcQ"

    def test_embed_url(self):
        assert extract_video_id(
            "https://www.youtube.com/embed/dQw4w9WgXcQ"
        ) == "dQw4w9WgXcQ"

    def test_strips_extra_query_params(self):
        # ?t=42&list=PLABC also has v=, so the ID still extracts correctly.
        assert extract_video_id(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42&list=PLabc123"
        ) == "dQw4w9WgXcQ"

    def test_rejects_invalid_id_length(self):
        # YouTube video IDs are exactly 11 chars.
        assert extract_video_id("https://www.youtube.com/watch?v=tooshort") is None
        assert extract_video_id(
            "https://www.youtube.com/watch?v=way_too_long_to_be_real"
        ) is None

    def test_no_video_id_in_playlist_url(self):
        # /playlist?list=... has no video ID.
        assert extract_video_id(
            "https://www.youtube.com/playlist?list=PLabcdef"
        ) is None

    def test_handles_trailing_garbage(self):
        assert extract_video_id(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ'"
        ) == "dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# is_playlist_url
# ---------------------------------------------------------------------------

class TestIsPlaylistUrl:
    def test_pure_playlist(self):
        assert is_playlist_url("https://www.youtube.com/playlist?list=PLabc123")

    def test_video_with_playlist_context_is_not_playlist(self):
        # A /watch URL with both v= and list= is a video, not a playlist.
        # (We treat the video as the canonical thing.)
        assert not is_playlist_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc123"
        )

    def test_rejects_video_url(self):
        assert not is_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_playlist_without_list_param_is_not_playlist(self):
        assert not is_playlist_url("https://www.youtube.com/playlist")


# ---------------------------------------------------------------------------
# normalize_youtube_url
# ---------------------------------------------------------------------------

class TestNormalizeYoutubeUrl:
    def test_canonical_form(self):
        assert (
            normalize_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
            == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_strips_tracking_params(self):
        # ?si=, ?t=, ?utm_source= are all noise.
        assert (
            normalize_youtube_url(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=abc&utm_source=email"
            )
            == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_short_link_normalizes_to_canonical(self):
        # youtu.be → www.youtube.com/watch?v=
        assert (
            normalize_youtube_url("https://youtu.be/dQw4w9WgXcQ?si=tracking")
            == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_mobile_normalizes_to_www(self):
        assert (
            normalize_youtube_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ")
            == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_shorts_normalizes_to_canonical(self):
        # /shorts/X → /watch?v=X. Same content, different surface.
        # Important for dedup: the same Short can be linked via either URL.
        assert (
            normalize_youtube_url("https://www.youtube.com/shorts/dQw4w9WgXcQ")
            == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_strips_trailing_garbage(self):
        # zsh dquote artifact: trailing single-quote.
        assert (
            normalize_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ'")
            == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_unrecognized_url_returned_minus_garbage(self):
        # No video ID extractable → returned with trailing-garbage stripped
        # but otherwise unchanged. Caller decides what to do.
        result = normalize_youtube_url("https://www.youtube.com/playlist?list=PLabc")
        assert "PLabc" in result  # not mangled
