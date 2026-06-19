"""Tests for media_archive.core.collections.ops (v0.3.0)."""
import datetime as _dt
import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    """Each test runs against a fresh SQLite DB. We reset the engine but
    do NOT reload the schemas module — reloading splits the SQLAlchemy
    class registry and breaks cross-class relationships (Video → Creator
    in particular)."""
    tmp = Path(tempfile.mkdtemp(prefix="ma-test-collections-"))
    monkeypatch.setenv("TT_DATA_DIR", str(tmp))
    monkeypatch.setenv("TT_DB_PATH", str(tmp / "test.db"))
    monkeypatch.setenv("TT_DB_URL", f"sqlite:///{tmp / 'test.db'}")
    monkeypatch.setenv("TT_LOG_LEVEL", "WARNING")
    import importlib
    import media_archive.core.config as cfg
    importlib.reload(cfg)
    # Reset engine so init_db() re-creates against the new DB_URL.
    # Do NOT reload schemas — that would split the SQLAlchemy registry.
    import media_archive.core.db.schemas as schemas
    if schemas._engine is not None:
        schemas._engine.dispose()
    schemas._engine = None
    schemas._SessionLocal = None
    # Force the schemas module to pick up the new DB_URL by re-reading it.
    schemas.DB_URL = cfg.DB_URL
    yield tmp


from media_archive.core.collections.ops import (
    CollectionAlreadyExistsError,
    CollectionError,
    CollectionNotFoundError,
    add_by_creator,
    add_by_topic,
    add_video_to_collection,
    create_collection,
    delete_collection,
    list_collections,
    remove_video_from_collection,
    show_collection,
)
from media_archive.core.db.schemas import Video, get_session, init_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_video(
    *, url, handle, summary="x", topics=None, key_points=None,
    transcript="some transcript text",
):
    """Insert a Video row with the JSON-serialized list columns
    matching what the analyze pipeline writes."""
    init_db()
    s = get_session()
    try:
        v = Video(
            url=url,
            source="test",
            collection_name="",
            platform="tiktok",
            video_id=url.split("/")[-1],
            author_handle=handle,
            download_status="downloaded",
            transcript=transcript,
            summary=summary,
            key_points=json.dumps(key_points or []),
            topics=json.dumps(topics or []),
            intent="entertain",
            tagged_at=_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None),
        )
        s.add(v)
        s.commit()
        return v.id
    finally:
        s.close()


# ---------------------------------------------------------------------------
# create / delete / list
# ---------------------------------------------------------------------------

class TestCreateAndList:
    def test_create_then_list(self):
        result = create_collection("test-coll", description="hello")
        assert result["name"] == "test-coll"
        assert result["member_count"] == 0

        items = list_collections()
        names = [c["name"] for c in items]
        assert "test-coll" in names

    def test_duplicate_name_rejected(self):
        create_collection("dup")
        with pytest.raises(CollectionAlreadyExistsError):
            create_collection("dup")

    def test_empty_name_rejected(self):
        with pytest.raises(CollectionError):
            create_collection("")
        with pytest.raises(CollectionError):
            create_collection("   ")

    def test_long_name_rejected(self):
        with pytest.raises(CollectionError):
            create_collection("x" * 200)

    def test_delete_collection_unlinks_members_keeps_videos(self):
        create_collection("doomed")
        vid = _add_video(url="https://www.tiktok.com/@u/video/9001", handle="u")
        add_video_to_collection("doomed", vid)

        removed = delete_collection("doomed")
        assert removed == 1

        # Video survives
        s = get_session()
        try:
            assert s.get(Video, vid) is not None
        finally:
            s.close()

        # Listing no longer shows it
        names = [c["name"] for c in list_collections()]
        assert "doomed" not in names


# ---------------------------------------------------------------------------
# add / remove
# ---------------------------------------------------------------------------

class TestAddRemove:
    def test_add_by_id(self):
        create_collection("by-id")
        vid = _add_video(url="https://www.tiktok.com/@a/video/1", handle="a")
        result = add_video_to_collection("by-id", vid)
        assert result["added"] is True
        assert result["video_id"] == vid
        assert result["position"] == 1

    def test_add_by_url(self):
        create_collection("by-url")
        url = "https://www.tiktok.com/@a/video/2"
        vid = _add_video(url=url, handle="a")
        result = add_video_to_collection("by-url", url)
        assert result["added"] is True
        assert result["video_id"] == vid

    def test_add_idempotent(self):
        create_collection("idem")
        vid = _add_video(url="https://www.tiktok.com/@a/video/3", handle="a")
        first = add_video_to_collection("idem", vid)
        assert first["added"] is True
        second = add_video_to_collection("idem", vid)
        assert second["added"] is False
        assert second["reason"] == "already_member"

    def test_position_increments(self):
        create_collection("pos")
        v1 = _add_video(url="https://www.tiktok.com/@a/video/4", handle="a")
        v2 = _add_video(url="https://www.tiktok.com/@a/video/5", handle="a")
        v3 = _add_video(url="https://www.tiktok.com/@a/video/6", handle="a")
        r1 = add_video_to_collection("pos", v1)
        r2 = add_video_to_collection("pos", v2)
        r3 = add_video_to_collection("pos", v3)
        assert (r1["position"], r2["position"], r3["position"]) == (1, 2, 3)

    def test_position_skips_after_remove(self):
        """Removing a member shouldn't compact positions of survivors,
        and a subsequent add should still get max+1 (= original last + 1)."""
        create_collection("gaps")
        v1 = _add_video(url="https://www.tiktok.com/@a/video/7", handle="a")
        v2 = _add_video(url="https://www.tiktok.com/@a/video/8", handle="a")
        v3 = _add_video(url="https://www.tiktok.com/@a/video/9", handle="a")
        add_video_to_collection("gaps", v1)
        add_video_to_collection("gaps", v2)
        remove_video_from_collection("gaps", v1)
        r3 = add_video_to_collection("gaps", v3)
        # v2 was at position 2 when v1 was removed; max stays at 2
        # (we don't rebuild on remove). v3 should get position 3.
        assert r3["position"] == 3

    def test_add_to_nonexistent_collection(self):
        vid = _add_video(url="https://www.tiktok.com/@a/video/10", handle="a")
        with pytest.raises(CollectionNotFoundError):
            add_video_to_collection("never-made", vid)

    def test_add_nonexistent_video(self):
        create_collection("vac")
        result = add_video_to_collection("vac", 99999)
        assert result["added"] is False
        assert result["reason"] == "video_not_found"

    def test_remove_nonmember_silently_returns_false(self):
        create_collection("rm")
        vid = _add_video(url="https://www.tiktok.com/@a/video/11", handle="a")
        result = remove_video_from_collection("rm", vid)
        assert result["removed"] is False
        assert result["reason"] == "not_member"


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

class TestShow:
    def test_show_returns_members_in_position_order(self):
        create_collection("ordered")
        v1 = _add_video(url="https://www.tiktok.com/@a/video/12", handle="a", summary="first")
        v2 = _add_video(url="https://www.tiktok.com/@a/video/13", handle="a", summary="second")
        v3 = _add_video(url="https://www.tiktok.com/@a/video/14", handle="a", summary="third")
        add_video_to_collection("ordered", v1)
        add_video_to_collection("ordered", v2)
        add_video_to_collection("ordered", v3)

        data = show_collection("ordered")
        assert data["name"] == "ordered"
        assert data["member_count"] == 3
        summaries = [m["summary"] for m in data["members"]]
        assert summaries == ["first", "second", "third"]

    def test_show_decodes_json_list_columns(self):
        """The summary path must JSON-decode key_points/topics so the
        formatter sees real lists, not blobs."""
        create_collection("decoded")
        vid = _add_video(
            url="https://www.tiktok.com/@a/video/15",
            handle="a",
            topics=["security", "embedded"],
            key_points=["point one", "point two"],
        )
        add_video_to_collection("decoded", vid)
        data = show_collection("decoded")
        m = data["members"][0]
        assert m["topics"] == ["security", "embedded"]
        assert m["key_points"] == ["point one", "point two"]

    def test_show_missing_collection(self):
        with pytest.raises(CollectionNotFoundError):
            show_collection("never-made-2")


# ---------------------------------------------------------------------------
# bulk-add
# ---------------------------------------------------------------------------

class TestBulkAdd:
    def test_add_by_creator_picks_up_handle_videos_only(self):
        create_collection("by-creator")
        _add_video(url="https://www.tiktok.com/@target/video/16", handle="target")
        _add_video(url="https://www.tiktok.com/@target/video/17", handle="target")
        _add_video(url="https://www.tiktok.com/@other/video/18", handle="other")

        result = add_by_creator("by-creator", "target")
        assert result["matched"] == 2
        assert result["added"] == 2
        assert result["skipped"] == 0

    def test_add_by_creator_strips_at_sign(self):
        """User pastes @handle with the @; we should still match."""
        create_collection("at-sign")
        _add_video(url="https://www.tiktok.com/@x/video/19", handle="x")
        result = add_by_creator("at-sign", "@x")
        assert result["added"] == 1

    def test_add_by_creator_idempotent(self):
        create_collection("idem-creator")
        _add_video(url="https://www.tiktok.com/@y/video/20", handle="y")
        first = add_by_creator("idem-creator", "y")
        second = add_by_creator("idem-creator", "y")
        assert first["added"] == 1
        assert second["added"] == 0
        assert second["skipped"] == 1

    def test_add_by_topic_matches_substring_case_insensitive(self):
        create_collection("by-topic")
        _add_video(
            url="https://www.tiktok.com/@a/video/21",
            handle="a",
            topics=["Cybersecurity", "embedded"],
        )
        _add_video(
            url="https://www.tiktok.com/@a/video/22",
            handle="a",
            topics=["cooking"],
        )
        result = add_by_topic("by-topic", "cyber")  # lowercase substring
        assert result["added"] == 1
        assert result["skipped"] == 0

    def test_add_by_topic_handles_invalid_json_gracefully(self):
        """If a stale Video row has malformed topics blob, skip it
        rather than crashing. (Defensive against pre-v0.2.1 rows.)"""
        create_collection("malformed")
        s = get_session()
        try:
            v = Video(
                url="https://www.tiktok.com/@a/video/23",
                source="test",
                collection_name="",
                platform="tiktok",
                video_id="23",
                author_handle="a",
                download_status="downloaded",
                topics="this is not JSON",
            )
            s.add(v)
            s.commit()
        finally:
            s.close()

        # Should not crash even though row has garbage in topics blob.
        result = add_by_topic("malformed", "x")
        assert result["added"] == 0
