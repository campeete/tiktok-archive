"""Local semantic + keyword search index for transcripts (v0.4.0)."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

from media_archive.core import config
from media_archive.core.db.schemas import Video, session_scope

logger = logging.getLogger(__name__)

_MODEL_NAME = getattr(config, "EMBED_MODEL", "all-MiniLM-L6-v2")
_INDEX_PATH = Path(getattr(config, "DATA_DIR", Path.home() / ".media-archive")) / "embeddings.npz"
_model = None


def _get_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError("Run: pip install sentence-transformers") from e
        logger.info("loading embedding model %s", _MODEL_NAME)
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _load_index():
    if not _INDEX_PATH.exists():
        return np.empty((0,), dtype=np.int64), np.empty((0, 0), dtype=np.float32)
    data = np.load(_INDEX_PATH)
    return data["ids"], data["vectors"]


def _save_index(ids, vectors):
    _INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.savez(_INDEX_PATH, ids=ids, vectors=vectors)


def _normalize(m):
    if m.size == 0:
        return m
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def embed_pending(limit: int = 100) -> int:
    import datetime as _dt
    model = _get_model()
    ids_existing, vecs_existing = _load_index()
    already = set(int(i) for i in ids_existing.tolist())
    new_ids, new_texts = [], []
    with session_scope() as session:
        rows = (
            session.query(Video)
            .filter(Video.transcript.isnot(None))
            .filter(Video.transcript != "")
            .filter(Video.embedded_at.is_(None))
            .limit(limit)
            .all()
        )
        for v in rows:
            if v.id in already:
                continue
            new_ids.append(v.id)
            new_texts.append(v.transcript)
        if not new_ids:
            return 0
        new_vecs = model.encode(new_texts, batch_size=32, show_progress_bar=False).astype(np.float32)
        if vecs_existing.size == 0:
            ids_out, vecs_out = np.array(new_ids, dtype=np.int64), new_vecs
        else:
            ids_out = np.concatenate([ids_existing, np.array(new_ids, dtype=np.int64)])
            vecs_out = np.vstack([vecs_existing, new_vecs])
        _save_index(ids_out, vecs_out)
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        nid = set(new_ids)
        for v in rows:
            if v.id in nid:
                v.embedded_at = now
    logger.info("embedded %d videos", len(new_ids))
    return len(new_ids)


_WORD_RE = re.compile(r"\w+")


def _keyword_bonus(query, transcript):
    q = {w.lower() for w in _WORD_RE.findall(query)}
    if not q:
        return 0.0
    t = {w.lower() for w in _WORD_RE.findall(transcript)}
    return 0.2 * (len(q & t) / len(q))


def _snippet(transcript, query, width=240):
    q_terms = [w.lower() for w in _WORD_RE.findall(query)]
    low = transcript.lower()
    pos = -1
    for term in q_terms:
        pos = low.find(term)
        if pos != -1:
            break
    if pos == -1:
        return transcript[:width].strip() + ("…" if len(transcript) > width else "")
    start = max(0, pos - width // 2)
    end = min(len(transcript), start + width)
    return ("…" if start > 0 else "") + transcript[start:end].strip() + ("…" if end < len(transcript) else "")


def search(query: str, k: int = 10) -> list[dict]:
    if not query.strip():
        return []
    ids, vectors = _load_index()
    if ids.size == 0:
        return []
    model = _get_model()
    q_vec = _normalize(model.encode([query]).astype(np.float32))[0]
    mat = _normalize(vectors)
    sims = mat @ q_vec
    top_n = min(len(sims), max(k * 3, k))
    cand_idx = np.argpartition(-sims, top_n - 1)[:top_n]
    results = []
    with session_scope() as session:
        for i in cand_idx:
            vid = int(ids[i])
            v = session.get(Video, vid)
            if v is None or not v.transcript:
                continue
            score = float(sims[i]) + _keyword_bonus(query, v.transcript)
            results.append({"video_id": vid, "score": round(score, 4), "snippet": _snippet(v.transcript, query)})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:k]


def chroma_available():
    try:
        import sentence_transformers  # noqa: F401
        return True, "OK (local sentence-transformers)"
    except ImportError:
        return False, "sentence-transformers not installed"


def embed_available():
    return chroma_available()
