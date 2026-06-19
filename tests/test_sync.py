"""Tests for media_archive.sources.tiktok.sync — creator management, no network."""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="tt-test-sync-"))
    monkeypatch.setenv("TT_DATA_DIR", str(tmp))
    monkeypatch.setenv("TT_DB_PATH", str(tmp / "test.db"))
    monkeypatch.setenv("TT_DB_URL", f"sqlite:///{tmp / 'test.db'}")
    monkeypatch.setenv("TT_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("TT_CREATORS_PATH", str(tmp / "creators.yaml"))
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
    import media_archive.sources.tiktok.sync as sync_mod
    importlib.reload(sync_mod)
    yield tmp


def test_add_creator_basic():
    from media_archive.sources.tiktok.sync import add_creator
    c = add_creator("alice")
    assert c.handle == "alice"
    assert c.enabled is True
    assert c.profile_url == "https://www.tiktok.com/@alice"


def test_add_creator_normalizes_at():
    from media_archive.sources.tiktok.sync import add_creator
    c = add_creator("@bob")
    assert c.handle == "bob"


def test_add_creator_idempotent():
    from media_archive.sources.tiktok.sync import add_creator, list_creators
    add_creator("alice", display_name="Alice First")
    add_creator("alice", display_name="Alice Second", notes="updated")
    creators = list_creators()
    assert len(creators) == 1
    assert creators[0].display_name == "Alice Second"
    assert creators[0].notes == "updated"


def test_disable_creator():
    from media_archive.sources.tiktok.sync import add_creator, disable_creator, list_creators
    add_creator("alice")
    assert disable_creator("alice") is True
    assert disable_creator("nonexistent") is False
    c = list_creators()[0]
    assert c.enabled is False


def test_remove_creator():
    from media_archive.sources.tiktok.sync import add_creator, list_creators, remove_creator
    add_creator("alice")
    assert remove_creator("alice") is True
    assert list_creators() == []


def test_list_creators_sorted_by_handle():
    from media_archive.sources.tiktok.sync import add_creator, list_creators
    add_creator("zach")
    add_creator("alice")
    add_creator("mark")
    handles = [c.handle for c in list_creators()]
    assert handles == ["alice", "mark", "zach"]


def test_export_to_yaml(tmp_path, monkeypatch):
    from media_archive.sources.tiktok.sync import add_creator, export_to_yaml
    add_creator("alice", notes="cybersec")
    add_creator("bob", sync_depth="last-50")

    out = tmp_path / "creators.yaml"
    export_to_yaml(out)
    text = out.read_text()
    assert "alice" in text
    assert "bob" in text
    assert "last-50" in text
    assert "cybersec" in text


def test_import_from_yaml(tmp_path):
    from media_archive.sources.tiktok.sync import import_from_yaml, list_creators
    yaml_path = tmp_path / "creators.yaml"
    yaml_path.write_text("""
creators:
  - handle: alice
    display_name: Alice Cooper
    sync_depth: full
    notes: rock
  - handle: '@bob'
  - handle: charlie
    sync_depth: last-50
""")
    result = import_from_yaml(yaml_path)
    assert result["added"] == 3
    assert result["errors"] == 0
    handles = sorted(c.handle for c in list_creators())
    assert handles == ["alice", "bob", "charlie"]


def test_import_from_yaml_missing_file():
    from media_archive.sources.tiktok.sync import import_from_yaml
    result = import_from_yaml(Path("/does/not/exist.yaml"))
    assert result.get("missing_file") is True


def test_import_yaml_then_export_roundtrip(tmp_path):
    from media_archive.sources.tiktok.sync import export_to_yaml, import_from_yaml, list_creators
    src = tmp_path / "in.yaml"
    src.write_text("""
creators:
  - handle: alice
    display_name: Alice
    sync_depth: last-6mo
    notes: testing
""")
    import_from_yaml(src)
    dst = tmp_path / "out.yaml"
    export_to_yaml(dst)
    # Re-importing the exported file should be a no-op (idempotent)
    result = import_from_yaml(dst)
    assert result["added"] == 0
    assert result["updated"] == 1
