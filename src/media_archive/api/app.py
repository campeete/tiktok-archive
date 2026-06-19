"""FastAPI HTTP layer for media-archive (v0.4.0).

Thin wrapper over the existing core: each route hands the request to a
function that is already tested, and returns JSON. No business logic here.
"""
from __future__ import annotations

from fastapi import FastAPI, Query

from media_archive.core.index import embed as _embed

app = FastAPI(title="media-archive", version="0.4.0")


@app.get("/health", tags=["meta"])
def health():
    ok, detail = _embed.embed_available()
    return {"status": "ok", "version": "0.4.0", "search_backend": detail}


@app.get("/search", tags=["search"])
def search(q: str = Query(..., min_length=1), k: int = 10):
    """Semantic + keyword search across the archive. Wraps core embed.search()."""
    return {"query": q, "results": _embed.search(q, k=k)}
