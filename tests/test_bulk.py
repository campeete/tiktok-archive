"""Tests for `tiktok-archive analyze-bulk`."""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="tt-test-bulk-"))
    monkeypatch.setenv("TT_DATA_DIR", str(tmp))
    monkeypatch.setenv("TT_DB_PATH", str(tmp / "test.db"))
    monkeypatch.setenv("TT_DB_URL", f"sqlite:///{tmp / 'test.db'}")
    monkeypatch.setenv("TT_LOG_LEVEL", "WARNING")
    import importlib
    import media_archive.core.config as cfg
    importlib.reload(cfg)
    # v0.3.0: do NOT reload schemas — reloading splits the SQLAlchemy
    # class registry and breaks Video<->Creator (and Collection<->Video)
    # relationships in any test that runs after this one. Just reset
    # the engine so init_db() picks up the new DB_URL.
    import media_archive.core.db.schemas as schemas
    if schemas._engine is not None:
        schemas._engine.dispose()
    schemas._engine = None
    schemas._SessionLocal = None
    schemas.DB_URL = cfg.DB_URL
    import media_archive.core.queue as q
    importlib.reload(q)
    yield tmp


def test_bulk_enqueue_inserts_jobs(tmp_path):
    """Writing 3 URLs to a file and bulk-enqueueing should create 3 jobs + 3 videos."""
    from media_archive.cli import _run_bulk_enqueue
    from media_archive.core.db.schemas import Job, Video, get_session, init_db

    init_db()
    # v0.2.0 changed _run_bulk_enqueue to accept (platform, url) tuples so
    # the dispatcher knows which source plugin processed each row.
    urls = [
        ("tiktok", "https://www.tiktok.com/@user1/video/111"),
        ("tiktok", "https://www.tiktok.com/@user2/video/222"),
        ("tiktok", "https://www.tiktok.com/@user3/photo/333"),
    ]
    rc = _run_bulk_enqueue(urls)
    assert rc == 0

    session = get_session()
    try:
        videos = session.query(Video).all()
        jobs = session.query(Job).all()
        assert len(videos) == 3
        assert len(jobs) == 3
        assert all(v.source == "bulk" for v in videos)
        assert all(v.platform == "tiktok" for v in videos)
        assert all(j.kind == "download" for j in jobs)
        # Photo URL got the right ID extracted
        photo_video = next(v for v in videos if "/photo/" in v.url)
        assert photo_video.video_id == "333"
        assert photo_video.author_handle == "user3"
    finally:
        session.close()


def test_bulk_enqueue_dedupes_already_completed(tmp_path):
    """If a URL was already analyzed (tagged_at set), don't re-enqueue it."""
    import datetime as _dt

    from media_archive.cli import _run_bulk_enqueue
    from media_archive.core.db.schemas import Job, Video, get_session, init_db

    init_db()
    # Pre-insert a "completed" video
    session = get_session()
    try:
        v = Video(
            url="https://www.tiktok.com/@u/video/999",
            source="bulk",
            collection_name="",
            tagged_at=_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None),
        )
        session.add(v)
        session.commit()
    finally:
        session.close()

    rc = _run_bulk_enqueue([
        ("tiktok", "https://www.tiktok.com/@u/video/999"),   # should be skipped
        ("tiktok", "https://www.tiktok.com/@u/video/1000"),  # new, should be enqueued
    ])
    assert rc == 0

    session = get_session()
    try:
        videos = session.query(Video).all()
        jobs = session.query(Job).all()
        assert len(videos) == 2
        assert len(jobs) == 1  # only one new video was enqueued
        # The job should point at the new video
        new_video = next(v for v in videos if v.url.endswith("/1000"))
        assert jobs[0].video_id == new_video.id
    finally:
        session.close()


def test_bulk_enqueue_idempotent_with_pending_jobs():
    """Running bulk twice on the same URLs should not duplicate jobs."""
    from media_archive.cli import _run_bulk_enqueue
    from media_archive.core.db.schemas import Job, Video, get_session, init_db

    init_db()
    urls = [
        ("tiktok", "https://www.tiktok.com/@u/video/100"),
        ("tiktok", "https://www.tiktok.com/@u/video/200"),
    ]

    rc1 = _run_bulk_enqueue(urls)
    assert rc1 == 0
    rc2 = _run_bulk_enqueue(urls)
    assert rc2 == 0

    session = get_session()
    try:
        videos = session.query(Video).all()
        jobs = session.query(Job).all()
        # Should be 2 videos and 2 jobs total — second run is a no-op
        assert len(videos) == 2
        assert len(jobs) == 2
    finally:
        session.close()


def test_bulk_command_parses_url_file(tmp_path, capsys, monkeypatch):
    """The cmd_analyze_bulk function should parse URLs, ignore comments, dedupe, and enqueue."""
    from media_archive.cli import cmd_analyze_bulk
    from media_archive.core.db.schemas import Video, get_session, init_db

    init_db()
    url_file = tmp_path / "urls.txt"
    url_file.write_text("""\
# This is a comment
https://www.tiktok.com/@a/video/1
https://www.tiktok.com/@b/photo/2  # inline comment after a URL

# blank line above is fine

https://www.tiktok.com/@a/video/1   # duplicate, should be deduped
not-a-url-line
""")

    class Args:
        file = str(url_file)
        inline = False
        keep_video = False
    rc = cmd_analyze_bulk(Args())
    assert rc == 0

    out = capsys.readouterr().out
    # v0.2.0 changed the wording from "Found N TikTok URLs (M unique)" to
    # "Found M URLs (N tiktok) in PATH" — the inner number is now the
    # post-dedup unique count and the per-platform breakdown sits in parens.
    assert "Found 2 URLs" in out
    assert "2 tiktok" in out
    # Unparseable line should be reported separately.
    assert "1 unparseable" in out

    session = get_session()
    try:
        videos = session.query(Video).all()
        assert len(videos) == 2
    finally:
        session.close()


def test_bulk_command_rejects_missing_file(tmp_path, capsys):
    from media_archive.cli import cmd_analyze_bulk

    class Args:
        file = str(tmp_path / "does-not-exist.txt")
        inline = False
        keep_video = False
    rc = cmd_analyze_bulk(Args())
    assert rc == 2


# ---------- v0.2.0: bulk file-parsing handles multiple platforms ----------
#
# The v1.7.3 version of these tests checked that YouTube URLs were rejected.
# v0.2.0 accepts YouTube URLs alongside TikTok. The "rejected" bucket is
# now URLs whose host isn't tiktok.com or youtube.com (e.g. instagram.com,
# vimeo.com, raw IPs, etc.).

def _run_cmd_analyze_bulk(file_path, *, inline=True, keep_video=False):
    """Helper: build an argparse Namespace and call cmd_analyze_bulk.

    Patches BOTH source-plugin analyzers since v0.2.0 dispatches to
    whichever one matches the URL. mock_tiktok and mock_youtube are
    returned separately so callers can assert per-platform call counts.
    """
    import argparse
    from media_archive.cli import cmd_analyze_bulk
    from unittest.mock import patch
    args = argparse.Namespace(
        file=str(file_path),
        inline=inline,
        keep_video=keep_video,
    )
    with patch("media_archive.sources.tiktok.process.analyze.analyze_url") as mock_tt, \
         patch("media_archive.sources.youtube.process.analyze.analyze_url") as mock_yt:
        mock_tt.return_value = {"ok": True, "elapsed_sec": 0.1, "summary": "tt-x"}
        mock_yt.return_value = {"ok": True, "elapsed_sec": 0.1, "summary": "yt-x"}
        rc = cmd_analyze_bulk(args)
    return rc, mock_tt, mock_yt


def test_bulk_accepts_tiktok_and_youtube(tmp_path, capsys):
    """A mixed-source file should route TikTok URLs to the TikTok analyzer
    and YouTube URLs to the YouTube analyzer.

    This is the core v0.2.0 multi-source behavior.
    """
    f = tmp_path / "urls.txt"
    f.write_text(
        "https://www.tiktok.com/@user1/video/111\n"
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
        "https://youtu.be/abcdefghijk\n"
        "https://www.tiktok.com/@user2/video/222\n"
    )
    rc, mock_tt, mock_yt = _run_cmd_analyze_bulk(f)
    out = capsys.readouterr().out

    # Each analyzer should have been called for its platform's URLs.
    assert mock_tt.call_count == 2
    assert mock_yt.call_count == 2
    # The header line should report both platforms.
    assert "2 tiktok" in out
    assert "2 youtube" in out


def test_bulk_rejects_unsupported_hosts(tmp_path, capsys):
    """URLs from hosts other than tiktok.com / youtube.com should be
    skipped with a clear report. Instagram, Vimeo, etc. land here until
    they get their own source plugin."""
    f = tmp_path / "urls.txt"
    f.write_text(
        "https://www.tiktok.com/@user1/video/111\n"
        "https://www.instagram.com/reel/abc/\n"
        "https://vimeo.com/123456789\n"
        "https://www.tiktok.com/@user2/video/222\n"
    )
    rc, mock_tt, mock_yt = _run_cmd_analyze_bulk(f)
    out = capsys.readouterr().out

    # Should have analyzed exactly the two TikTok URLs.
    assert mock_tt.call_count == 2
    assert mock_yt.call_count == 0
    # Should have reported what got skipped.
    assert "Skipped 2 unsupported URL" in out
    assert "instagram.com" in out
    assert "vimeo.com" in out


def test_bulk_rejects_unparseable_lines(tmp_path, capsys):
    """Lines that aren't URLs and aren't comments should be reported."""
    f = tmp_path / "urls.txt"
    f.write_text(
        "https://www.tiktok.com/@user1/video/111\n"
        "this is not a url\n"
        "another bogus line\n"
        "# this is a real comment, should be silent\n"
    )
    rc, mock_tt, mock_yt = _run_cmd_analyze_bulk(f)
    out = capsys.readouterr().out

    assert mock_tt.call_count == 1
    assert mock_yt.call_count == 0
    # Two unparseable lines reported.
    assert "Skipped 2 unparseable line" in out
    # Comment-only lines should NOT be reported as rejected.
    assert "real comment" not in out


def test_bulk_with_only_invalid_urls_returns_error(tmp_path, capsys):
    """All-bad-URL file: don't proceed to analysis, return non-zero."""
    f = tmp_path / "urls.txt"
    f.write_text(
        "https://www.instagram.com/reel/X/\n"
        "this is not a url\n"
    )
    rc, mock_tt, mock_yt = _run_cmd_analyze_bulk(f)
    err = capsys.readouterr().err

    assert mock_tt.call_count == 0
    assert mock_yt.call_count == 0
    assert rc != 0
    assert "No valid URLs" in err


def test_bulk_strips_trailing_punctuation_via_normalize(tmp_path, capsys):
    """A pasted URL with a trailing single-quote (zsh dquote artifact)
    should still be accepted — normalize_tiktok_url strips the quote
    after is_tiktok_url accepts it."""
    f = tmp_path / "urls.txt"
    f.write_text(
        "https://www.tiktok.com/@user1/video/111'\n"
    )
    rc, mock_tt, mock_yt = _run_cmd_analyze_bulk(f)

    assert mock_tt.call_count == 1
    # The URL passed to analyze_url should have the quote stripped.
    called_url = mock_tt.call_args[0][0]
    assert called_url == "https://www.tiktok.com/@user1/video/111"
