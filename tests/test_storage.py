"""Tests for media_archive.core.storage — LocalStorage and MirroredStorage."""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmpdir():
    return Path(tempfile.mkdtemp(prefix="tt-test-storage-"))


def test_local_put_get(tmpdir):
    from media_archive.core.storage import LocalStorage
    s = LocalStorage(tmpdir)
    s.put("foo/bar.txt", b"hello")
    assert s.get("foo/bar.txt") == b"hello"


def test_local_put_overwrite(tmpdir):
    from media_archive.core.storage import LocalStorage
    s = LocalStorage(tmpdir)
    s.put("a.txt", b"first")
    s.put("a.txt", b"second")
    assert s.get("a.txt") == b"second"


def test_local_get_missing_key_raises(tmpdir):
    from media_archive.core.storage import LocalStorage
    s = LocalStorage(tmpdir)
    with pytest.raises(KeyError):
        s.get("nope.txt")


def test_local_exists(tmpdir):
    from media_archive.core.storage import LocalStorage
    s = LocalStorage(tmpdir)
    assert s.exists("a.txt") is False
    s.put("a.txt", b"x")
    assert s.exists("a.txt") is True


def test_local_delete(tmpdir):
    from media_archive.core.storage import LocalStorage
    s = LocalStorage(tmpdir)
    s.put("a.txt", b"x")
    assert s.delete("a.txt") is True
    assert s.delete("a.txt") is False  # idempotent


def test_local_list_with_prefix(tmpdir):
    from media_archive.core.storage import LocalStorage
    s = LocalStorage(tmpdir)
    s.put("transcripts/1.json", b"a")
    s.put("transcripts/2.json", b"b")
    s.put("backups/db.sqlite", b"c")
    transcripts = sorted(s.list("transcripts"))
    assert transcripts == ["transcripts/1.json", "transcripts/2.json"]


def test_local_path_traversal_rejected(tmpdir):
    from media_archive.core.storage import LocalStorage
    s = LocalStorage(tmpdir)
    with pytest.raises(ValueError):
        s.put("../escape.txt", b"x")


def test_mirrored_put_writes_both():
    from media_archive.core.storage import LocalStorage, MirroredStorage
    primary_dir = Path(tempfile.mkdtemp(prefix="tt-pri-"))
    mirror_dir = Path(tempfile.mkdtemp(prefix="tt-mir-"))
    primary = LocalStorage(primary_dir)
    mirror = LocalStorage(mirror_dir)
    s = MirroredStorage(primary, mirror)
    s.put("k.txt", b"data")
    assert primary.get("k.txt") == b"data"
    assert mirror.get("k.txt") == b"data"


def test_mirrored_get_falls_back_to_mirror():
    from media_archive.core.storage import LocalStorage, MirroredStorage
    primary_dir = Path(tempfile.mkdtemp(prefix="tt-pri-"))
    mirror_dir = Path(tempfile.mkdtemp(prefix="tt-mir-"))
    primary = LocalStorage(primary_dir)
    mirror = LocalStorage(mirror_dir)
    mirror.put("only-in-mirror.txt", b"recovered")
    s = MirroredStorage(primary, mirror)
    assert s.get("only-in-mirror.txt") == b"recovered"


def test_mirrored_put_succeeds_even_if_mirror_fails():
    """If the mirror raises on put, primary still gets written."""
    from media_archive.core.storage import LocalStorage, MirroredStorage, StorageBackend

    class FailingMirror(StorageBackend):
        def put(self, key, data): raise RuntimeError("network down")
        def get(self, key): raise KeyError(key)
        def delete(self, key): return False
        def exists(self, key): return False
        def list(self, prefix=""): return iter([])

    primary_dir = Path(tempfile.mkdtemp(prefix="tt-pri-"))
    primary = LocalStorage(primary_dir)
    s = MirroredStorage(primary, FailingMirror())
    s.put("k.txt", b"data")  # should NOT raise
    assert primary.get("k.txt") == b"data"


def test_make_storage_returns_local_by_default(monkeypatch, tmpdir):
    monkeypatch.setenv("TT_STORAGE_BACKEND", "local")
    monkeypatch.setenv("TT_DATA_DIR", str(tmpdir))
    import importlib
    import media_archive.core.config as cfg
    importlib.reload(cfg)
    import media_archive.core.storage as storage_mod
    importlib.reload(storage_mod)
    s = storage_mod.make_storage()
    assert isinstance(s, storage_mod.LocalStorage)


def test_test_r2_connection_when_not_configured(monkeypatch):
    monkeypatch.setenv("TT_STORAGE_BACKEND", "local")
    import importlib
    import media_archive.core.config as cfg
    importlib.reload(cfg)
    import media_archive.core.storage as storage_mod
    importlib.reload(storage_mod)
    ok, msg = storage_mod.test_r2_connection()
    assert ok is False
    assert "not 'r2'" in msg
