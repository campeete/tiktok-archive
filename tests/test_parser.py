"""Tests for media_archive.sources.tiktok.ingest.parser — pure data, no network."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    """Run every test against a fresh temp data dir."""
    tmp = Path(tempfile.mkdtemp(prefix="tt-test-"))
    monkeypatch.setenv("TT_DATA_DIR", str(tmp))
    monkeypatch.setenv("TT_DB_PATH", str(tmp / "test.db"))
    monkeypatch.setenv("TT_DB_URL", f"sqlite:///{tmp / 'test.db'}")
    monkeypatch.setenv("TT_LOG_LEVEL", "WARNING")
    # Force a re-import so config picks up the env vars
    import importlib
    import media_archive.core.config as cfg
    importlib.reload(cfg)
    import media_archive.core.db.schemas as schemas
    schemas._engine = None
    schemas._SessionLocal = None
    # v0.3.0: do not reload schemas, just dispose the engine so init_db re-reads DB_URL
    if schemas._engine is not None:
        schemas._engine.dispose()
    schemas._engine = None
    schemas._SessionLocal = None
    schemas.DB_URL = cfg.DB_URL
    yield tmp


def make_export(tmp: Path, payload: dict) -> Path:
    """Write a fake export JSON to tmp and return its path."""
    p = tmp / "export.json"
    p.write_text(json.dumps(payload))
    return p


def test_ingest_empty_export(isolated_data_dir):
    from media_archive.sources.tiktok.ingest.parser import ingest_export
    p = make_export(isolated_data_dir, {})
    result = ingest_export(p)
    assert result == {"added": 0, "skipped": 0, "errors": 0}


def test_ingest_liked_videos(isolated_data_dir):
    from media_archive.sources.tiktok.ingest.parser import ingest_export
    payload = {
        "Activity": {
            "Like List": {
                "ItemFavoriteList": [
                    {"Date": "2024-01-15 10:00:00", "Link": "https://www.tiktok.com/@user1/video/111"},
                    {"Date": "2024-01-16 11:00:00", "Link": "https://www.tiktok.com/@user2/video/222"},
                    {"Date": "2024-01-17 12:00:00", "Link": "https://m.tiktok.com/@user3/video/333"},
                ]
            }
        }
    }
    p = make_export(isolated_data_dir, payload)
    result = ingest_export(p)
    assert result["added"] == 3
    assert result["skipped"] == 0


def test_ingest_dedupe(isolated_data_dir):
    """Re-ingesting the same export should skip duplicates."""
    from media_archive.sources.tiktok.ingest.parser import ingest_export
    payload = {
        "Activity": {
            "Like List": {
                "ItemFavoriteList": [
                    {"Date": "2024-01-15", "Link": "https://www.tiktok.com/@u/video/1"},
                ]
            }
        }
    }
    p = make_export(isolated_data_dir, payload)
    r1 = ingest_export(p)
    r2 = ingest_export(p)
    assert r1["added"] == 1
    assert r2["added"] == 0
    assert r2["skipped"] == 1


def test_ingest_drops_entries_without_url(isolated_data_dir):
    from media_archive.sources.tiktok.ingest.parser import ingest_export
    payload = {
        "Activity": {
            "Like List": {
                "ItemFavoriteList": [
                    {"Date": "2024-01-15"},  # no Link
                    {"Date": "2024-01-16", "Link": "https://www.tiktok.com/@u/video/1"},
                ]
            }
        }
    }
    p = make_export(isolated_data_dir, payload)
    result = ingest_export(p)
    assert result["added"] == 1


def test_extract_creators_groups_by_handle(isolated_data_dir):
    from media_archive.sources.tiktok.ingest.parser import extract_creators_from_export
    payload = {
        "Activity": {
            "Like List": {
                "ItemFavoriteList": [
                    {"Link": "https://www.tiktok.com/@alice/video/1"},
                    {"Link": "https://www.tiktok.com/@alice/video/2"},
                    {"Link": "https://www.tiktok.com/@bob/video/3"},
                ]
            }
        }
    }
    p = make_export(isolated_data_dir, payload)
    creators = extract_creators_from_export(p)
    by_handle = {c["handle"]: c for c in creators}
    assert by_handle["alice"]["video_count"] == 2
    assert by_handle["bob"]["video_count"] == 1
    # Sorted desc by video_count
    assert creators[0]["handle"] == "alice"


def test_extract_creators_skips_unknown_handles(isolated_data_dir):
    from media_archive.sources.tiktok.ingest.parser import extract_creators_from_export
    payload = {
        "Activity": {
            "Like List": {
                "ItemFavoriteList": [
                    {"Link": "https://example.com/not-a-tiktok-url"},
                ]
            }
        }
    }
    p = make_export(isolated_data_dir, payload)
    creators = extract_creators_from_export(p)
    assert creators == []


def test_missing_file_raises(isolated_data_dir):
    from media_archive.sources.tiktok.ingest.parser import ingest_export
    with pytest.raises(FileNotFoundError):
        ingest_export(isolated_data_dir / "does-not-exist.json")
