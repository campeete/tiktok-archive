from __future__ import annotations
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from media_archive.api.app import app

client = TestClient(app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.4.0"
    assert "search_backend" in body


def test_search_requires_q():
    r = client.get("/search")
    assert r.status_code == 422


def test_search_returns_shape():
    r = client.get("/search", params={"q": "anything", "k": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "anything"
    assert isinstance(body["results"], list)
