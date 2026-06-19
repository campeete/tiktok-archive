from __future__ import annotations
import pytest

pytest.importorskip("sentence_transformers")
pytest.importorskip("numpy")

from media_archive.core.db.schemas import Video, session_scope
from media_archive.core.index import embed as embed_mod


@pytest.fixture(autouse=True)
def _fresh_index(tmp_path, monkeypatch):
    idx = tmp_path / "embeddings.npz"
    monkeypatch.setattr(embed_mod, "_INDEX_PATH", idx, raising=True)
    monkeypatch.setattr(embed_mod, "_model", None, raising=True)
    yield


def _add_video(url, transcript):
    with session_scope() as session:
        v = Video(url=url, transcript=transcript)
        session.add(v)
        session.flush()
        return v.id


def test_embed_pending_counts_only_unembedded():
    a = _add_video("u://a", "How to bake sourdough bread at home with a starter.")
    b = _add_video("u://b", "Setting up a Tailscale mesh network for remote SSH.")
    n = embed_mod.embed_pending()
    assert n == 2
    assert embed_mod.embed_pending() == 0
    with session_scope() as session:
        assert session.get(Video, a).embedded_at is not None
        assert session.get(Video, b).embedded_at is not None


def test_search_ranks_relevant_first():
    _add_video("u://cook", "A recipe for baking sourdough bread with a starter.")
    _add_video("u://net", "Configuring a Tailscale VPN and SSH key authentication.")
    embed_mod.embed_pending()
    results = embed_mod.search("how do I make bread", k=2)
    assert results
    top = results[0]
    assert "bread" in top["snippet"].lower() or "sourdough" in top["snippet"].lower()
    assert top["score"] >= results[-1]["score"]


def test_search_empty_index_returns_empty():
    assert embed_mod.search("anything", k=5) == []


def test_search_blank_query_returns_empty():
    _add_video("u://x", "Some content here.")
    embed_mod.embed_pending()
    assert embed_mod.search("   ", k=5) == []


def test_result_shape():
    _add_video("u://shape", "Networking protocols and packet captures with Wireshark.")
    embed_mod.embed_pending()
    results = embed_mod.search("wireshark packets", k=1)
    assert len(results) == 1
    r = results[0]
    assert set(r.keys()) == {"video_id", "score", "snippet"}
    assert isinstance(r["video_id"], int)
    assert isinstance(r["score"], float)
    assert isinstance(r["snippet"], str)
