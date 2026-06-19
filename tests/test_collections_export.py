"""Tests for media_archive.core.collections.export (v0.3.0).

These tests work on hand-built dicts in the same shape that
ops.show_collection() produces, so they don't depend on the database.
"""
import datetime as _dt
import json

from media_archive.core.collections.export import (
    _format_duration,
    _truncate_words,
    export_collection,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _sample_collection(*, members=None):
    """Build a collection dict matching what show_collection returns."""
    return {
        "id": 1,
        "name": "test-coll",
        "description": "A test collection.",
        "created_at": _dt.datetime(2026, 5, 1, 12, 0, 0),
        "updated_at": _dt.datetime(2026, 5, 6, 14, 0, 0),
        "member_count": len(members or []),
        "members": members or [],
    }


def _sample_member(
    *,
    position=1,
    url="https://www.tiktok.com/@user/video/123",
    handle="user",
    summary="A short summary.",
    transcript="The full transcript text goes here. Word word word.",
    topics=None,
    key_points=None,
    duration=125.0,
    intent="entertain",
    platform="tiktok",
):
    return {
        "id": 1,
        "url": url,
        "platform": platform,
        "post_type": "video",
        "author_handle": handle,
        "author_display_name": None,
        "title": None,
        "duration_sec": duration,
        "upload_date": None,
        "summary": summary,
        "key_points": key_points or [],
        "topics": topics or [],
        "intent": intent,
        "claim_check": False,
        "transcript": transcript,
        "transcript_lang": "en",
        "is_important": False,
        "tagged_at": _dt.datetime(2026, 5, 6, 14, 0, 0),
        "position": position,
        "note": None,
    }


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------

class TestMarkdownExport:
    def test_empty_collection_renders(self):
        """Empty collection should still produce a valid markdown blob,
        not crash. The footer is what tells Claude this is empty."""
        coll = _sample_collection(members=[])
        out = export_collection(coll, format="md")
        assert "# Collection: test-coll" in out
        assert "empty" in out.lower()

    def test_compact_default_excludes_transcripts(self):
        """The whole point of the compact default — transcripts should
        NOT appear unless --full is set."""
        m = _sample_member(transcript="THIS_TRANSCRIPT_TEXT_SHOULD_NOT_APPEAR")
        coll = _sample_collection(members=[m])
        out = export_collection(coll, format="md", full_transcripts=False)
        assert "THIS_TRANSCRIPT_TEXT_SHOULD_NOT_APPEAR" not in out
        # But the summary should appear.
        assert "A short summary." in out

    def test_full_includes_transcripts(self):
        m = _sample_member(transcript="UNIQUE_TRANSCRIPT_MARKER_42")
        coll = _sample_collection(members=[m])
        out = export_collection(coll, format="md", full_transcripts=True)
        assert "UNIQUE_TRANSCRIPT_MARKER_42" in out

    def test_full_with_max_words_truncates(self):
        long_transcript = " ".join([f"word{i}" for i in range(100)])
        m = _sample_member(transcript=long_transcript)
        coll = _sample_collection(members=[m])
        out = export_collection(
            coll, format="md", full_transcripts=True, transcript_max_words=20,
        )
        assert "word0" in out
        assert "word19" in out
        assert "word99" not in out
        assert "[truncated]" in out

    def test_member_includes_topics_and_key_points(self):
        m = _sample_member(
            topics=["cybersecurity", "embedded"],
            key_points=["MIFARE Classic flaw", "HMAC-SHA256 mitigation"],
        )
        coll = _sample_collection(members=[m])
        out = export_collection(coll, format="md")
        assert "cybersecurity" in out
        assert "embedded" in out
        assert "MIFARE" in out
        assert "HMAC-SHA256" in out

    def test_member_position_appears_as_section_number(self):
        """Sections are numbered ## 1., ## 2., etc. so the LLM can
        reference posts unambiguously."""
        members = [
            _sample_member(position=1, handle="alpha"),
            _sample_member(position=2, handle="beta"),
            _sample_member(position=3, handle="gamma"),
        ]
        coll = _sample_collection(members=members)
        out = export_collection(coll, format="md")
        assert "## 1." in out
        assert "## 2." in out
        assert "## 3." in out
        # Order preserved
        alpha_idx = out.find("alpha")
        beta_idx = out.find("beta")
        gamma_idx = out.find("gamma")
        assert alpha_idx < beta_idx < gamma_idx

    def test_url_always_appears_in_compact(self):
        """The URL is the user's primary way to navigate back to source
        material. It MUST appear regardless of verbosity."""
        m = _sample_member(url="https://www.tiktok.com/@x/video/UNIQUE_URL_MARKER")
        coll = _sample_collection(members=[m])
        out = export_collection(coll, format="md", full_transcripts=False)
        assert "UNIQUE_URL_MARKER" in out

    def test_footer_describes_archive_to_llm(self):
        """The footer is what makes the export self-explanatory in a
        Claude conversation. Must mention the collection name and that
        this is a real curated archive."""
        coll = _sample_collection(members=[_sample_member()])
        out = export_collection(coll, format="md")
        assert "About this archive" in out
        assert "test-coll" in out
        assert "real" in out.lower() or "curated" in out.lower()


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

class TestJsonExport:
    def test_json_export_is_valid_json(self):
        coll = _sample_collection(members=[_sample_member()])
        out = export_collection(coll, format="json")
        # Must round-trip
        parsed = json.loads(out)
        assert parsed["name"] == "test-coll"
        assert parsed["member_count"] == 1

    def test_json_compact_strips_transcript(self):
        m = _sample_member(transcript="JSON_TRANSCRIPT_MARKER")
        coll = _sample_collection(members=[m])
        out = export_collection(coll, format="json", full_transcripts=False)
        parsed = json.loads(out)
        # Compact mode drops transcript field entirely from members
        assert "transcript" not in parsed["members"][0]

    def test_json_full_includes_transcript(self):
        m = _sample_member(transcript="JSON_TRANSCRIPT_MARKER")
        coll = _sample_collection(members=[m])
        out = export_collection(coll, format="json", full_transcripts=True)
        parsed = json.loads(out)
        assert parsed["members"][0]["transcript"] == "JSON_TRANSCRIPT_MARKER"

    def test_json_serializes_datetimes(self):
        """datetime objects in the dict need ISO-string conversion;
        otherwise json.dumps blows up."""
        coll = _sample_collection(members=[_sample_member()])
        # Must not crash.
        out = export_collection(coll, format="json")
        parsed = json.loads(out)
        # tagged_at is an ISO string after the round-trip
        assert isinstance(parsed["members"][0]["tagged_at"], str)


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

class TestFormatHelpers:
    def test_format_duration_seconds_only(self):
        assert _format_duration(45) == "0:45"

    def test_format_duration_minutes_seconds(self):
        assert _format_duration(125) == "2:05"

    def test_format_duration_hours_minutes_seconds(self):
        assert _format_duration(3725) == "1:02:05"

    def test_format_duration_handles_none(self):
        assert _format_duration(None) == ""

    def test_format_duration_handles_garbage(self):
        assert _format_duration("not-a-number") == ""

    def test_truncate_words_short_unchanged(self):
        assert _truncate_words("a b c", 10) == "a b c"

    def test_truncate_words_appends_marker(self):
        long = " ".join(str(i) for i in range(100))
        out = _truncate_words(long, 5)
        assert out.startswith("0 1 2 3 4")
        assert "[truncated]" in out


# ---------------------------------------------------------------------------
# Format selection / errors
# ---------------------------------------------------------------------------

def test_unknown_format_raises():
    coll = _sample_collection(members=[])
    try:
        export_collection(coll, format="xml")
        assert False, "should have raised"
    except ValueError as e:
        assert "format" in str(e).lower()


def test_text_format_strips_markdown():
    coll = _sample_collection(members=[_sample_member()])
    out = export_collection(coll, format="txt")
    # No # headers, no ** bold, no > blockquote markers
    assert "# " not in out
    assert "**" not in out
