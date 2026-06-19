"""Tests for media artifact extraction (Phase 1.7).

We can't run real ffmpeg/scenedetect/Pillow in CI tests reliably, so
we cover:
- The orchestration logic (which kinds get produced when, ordering).
- The MediaArtifact persistence.
- The drop_full_artifacts cleanup.
- Idempotency of re-extraction (UniqueConstraint on (video_id, kind, seq)).

End-to-end image extraction is exercised manually on Cameron's Mac.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    """Spin up a clean DB in a tempdir for each test."""
    tmp = Path(tempfile.mkdtemp(prefix="tt-test-media-"))
    monkeypatch.setenv("TT_DATA_DIR", str(tmp))
    monkeypatch.setenv("TT_DB_PATH", str(tmp / "test.db"))
    monkeypatch.setenv("TT_DB_URL", f"sqlite:///{tmp / 'test.db'}")
    monkeypatch.setenv("TT_MEDIA_DIR", str(tmp / "media"))
    monkeypatch.setenv("TT_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("TT_STORAGE_BACKEND", "local")  # don't try R2 in tests
    import importlib
    import media_archive.core.config as cfg
    importlib.reload(cfg)
    import media_archive.core.db.schemas as schemas
    # v0.3.0: do not reload schemas, just dispose the engine so init_db re-reads DB_URL
    if schemas._engine is not None:
        schemas._engine.dispose()
    schemas._engine = None
    schemas._SessionLocal = None
    schemas.DB_URL = cfg.DB_URL
    schemas.init_db()
    # Create a video row to attach artifacts to
    with schemas.session_scope() as s:
        v = schemas.Video(
            url="https://www.tiktok.com/@u/video/1",
            source="analyzed",
            video_id="1",
            author_handle="u",
        )
        s.add(v)
        s.flush()
        vid = v.id
    yield tmp, vid


def _make_jpeg(path: Path, color: tuple = (200, 100, 50)) -> bool:
    """Create a tiny real JPEG file for tests that don't need Pillow logic."""
    try:
        from PIL import Image
        im = Image.new("RGB", (640, 480), color=color)
        im.save(path, "JPEG", quality=85)
        return True
    except ImportError:
        # Pillow not installed in this environment — write 4 bytes that
        # vaguely look like a JPEG header so file ops succeed; tests that
        # need real image dims are skipped.
        path.write_bytes(b"\xff\xd8\xff\xe0")
        return False


def test_persist_artifact_creates_row_and_moves_file(fresh_db):
    """Core persistence: file moves from src to media dir, row inserted."""
    tmp, video_id = fresh_db
    from media_archive.sources.tiktok.process import media as media_module

    src = tmp / "src.jpg"
    _make_jpeg(src)
    assert src.exists()

    artifact = media_module._persist_artifact(
        video_id=video_id,
        kind="frame_thumb",
        sequence=0,
        src_path=src,
        timestamp_sec=2.0,
    )
    assert artifact is not None
    assert artifact.video_id == video_id
    assert artifact.kind == "frame_thumb"
    assert artifact.sequence == 0
    assert artifact.timestamp_sec == 2.0
    assert artifact.local_path is not None
    assert Path(artifact.local_path).exists()
    # Source moved (not copied)
    assert not src.exists()


def test_persist_artifact_idempotent_on_resequence(fresh_db):
    """Re-persisting (video_id, kind, seq) updates the existing row.

    This matters when scene-change extraction runs again after a video
    is unmarked-then-remarked important.
    """
    tmp, video_id = fresh_db
    from media_archive.sources.tiktok.process import media as media_module

    src1 = tmp / "src1.jpg"
    _make_jpeg(src1, color=(255, 0, 0))
    a1 = media_module._persist_artifact(
        video_id=video_id, kind="frame_full", sequence=2,
        src_path=src1, timestamp_sec=10.0,
    )
    assert a1 is not None
    first_id = a1.id

    src2 = tmp / "src2.jpg"
    _make_jpeg(src2, color=(0, 255, 0))
    a2 = media_module._persist_artifact(
        video_id=video_id, kind="frame_full", sequence=2,
        src_path=src2, timestamp_sec=11.5,
    )
    assert a2 is not None
    assert a2.id == first_id, "Re-persist must update, not insert"
    assert a2.timestamp_sec == 11.5


def test_drop_full_artifacts_removes_only_full(fresh_db):
    """When a user un-marks a video important, full-res artifacts are
    deleted but thumbnails stay (they were always going to be kept)."""
    tmp, video_id = fresh_db
    from media_archive.sources.tiktok.process import media as media_module
    from media_archive.core.db.schemas import MediaArtifact, session_scope

    for kind in ["frame_thumb", "frame_full", "slide_thumb", "slide_full"]:
        for seq in range(2):
            src = tmp / f"{kind}-{seq}.jpg"
            _make_jpeg(src)
            media_module._persist_artifact(
                video_id=video_id, kind=kind, sequence=seq,
                src_path=src, timestamp_sec=None,
            )

    deleted = media_module.drop_full_artifacts(video_id)
    assert deleted == 4  # 2 frame_full + 2 slide_full

    with session_scope() as s:
        remaining = s.query(MediaArtifact).filter_by(video_id=video_id).all()
        kinds = sorted({a.kind for a in remaining})
        assert kinds == ["frame_thumb", "slide_thumb"]


def test_extract_video_frames_skips_full_when_not_important(fresh_db, monkeypatch):
    """When is_important=False, only frame_thumb gets produced.

    Mocks ffmpeg + scenedetect helpers so we don't need real binaries.
    """
    tmp, video_id = fresh_db
    from media_archive.sources.tiktok.process import media as media_module
    from media_archive.core.db.schemas import MediaArtifact, session_scope

    # Pretend video file exists
    fake_video = tmp / "fake.mp4"
    fake_video.write_bytes(b"not really a video")

    def fake_uniform(video_path, output_dir, interval_sec=2.0):
        output_dir.mkdir(parents=True, exist_ok=True)
        out = []
        for i in range(3):
            p = output_dir / f"thumb-source-{i:03d}.jpg"
            _make_jpeg(p)
            out.append((p, i * interval_sec))
        return out

    def fake_scenes(video_path, output_dir, threshold=27.0):
        # Should NOT be called when is_important=False
        raise AssertionError("scene-change should not run for unimportant videos")

    monkeypatch.setattr(media_module, "_extract_uniform_frames", fake_uniform)
    monkeypatch.setattr(media_module, "_extract_scene_change_frames", fake_scenes)
    monkeypatch.setattr(media_module, "_pillow_available", lambda: True)
    # _thumbnail_image: just copy the source as the thumb (size doesn't matter for the test)
    def fake_thumb(src, dst, width=256):
        import shutil
        shutil.copy(str(src), str(dst))
        return (256, 192)
    monkeypatch.setattr(media_module, "_thumbnail_image", fake_thumb)

    result = media_module.extract_video_frames(
        video_id=video_id, video_path=fake_video, is_important=False,
    )

    assert result.thumbs_created == 3
    assert result.full_frames_created == 0

    with session_scope() as s:
        kinds = [a.kind for a in s.query(MediaArtifact).filter_by(video_id=video_id).all()]
        assert all(k == "frame_thumb" for k in kinds)
        assert len(kinds) == 3


def test_extract_video_frames_includes_full_when_important(fresh_db, monkeypatch):
    """When is_important=True, both uniform thumbs and scene frames run."""
    tmp, video_id = fresh_db
    from media_archive.sources.tiktok.process import media as media_module
    from media_archive.core.db.schemas import MediaArtifact, session_scope

    fake_video = tmp / "fake.mp4"
    fake_video.write_bytes(b"not really a video")

    def fake_uniform(video_path, output_dir, interval_sec=2.0):
        output_dir.mkdir(parents=True, exist_ok=True)
        p = output_dir / "thumb-source-001.jpg"
        _make_jpeg(p)
        return [(p, 0.0)]

    def fake_scenes(video_path, output_dir, threshold=27.0):
        output_dir.mkdir(parents=True, exist_ok=True)
        out = []
        for i in range(2):
            p = output_dir / f"frame-source-{i:03d}.jpg"
            _make_jpeg(p)
            out.append((p, float(i * 5)))
        return out

    monkeypatch.setattr(media_module, "_extract_uniform_frames", fake_uniform)
    monkeypatch.setattr(media_module, "_extract_scene_change_frames", fake_scenes)
    monkeypatch.setattr(media_module, "_pillow_available", lambda: True)
    def fake_thumb(src, dst, width=256):
        import shutil
        shutil.copy(str(src), str(dst))
        return (256, 192)
    monkeypatch.setattr(media_module, "_thumbnail_image", fake_thumb)

    result = media_module.extract_video_frames(
        video_id=video_id, video_path=fake_video, is_important=True,
    )

    assert result.thumbs_created == 1
    assert result.full_frames_created == 2

    with session_scope() as s:
        rows = s.query(MediaArtifact).filter_by(video_id=video_id).all()
        kinds = sorted([a.kind for a in rows])
        assert kinds == ["frame_full", "frame_full", "frame_thumb"]


def test_extract_photo_slides_thumbs_only_when_not_important(fresh_db, monkeypatch):
    """For photo posts, when not important: thumbs only, no full slides."""
    tmp, video_id = fresh_db
    from media_archive.sources.tiktok.process import media as media_module
    from media_archive.core.db.schemas import MediaArtifact, session_scope

    def fake_download(url, dst, timeout=30):
        _make_jpeg(dst)
        return True

    monkeypatch.setattr(media_module, "_pillow_available", lambda: True)
    monkeypatch.setattr(media_module, "_download_image_url", fake_download)
    def fake_thumb(src, dst, width=256):
        import shutil
        shutil.copy(str(src), str(dst))
        return (256, 192)
    monkeypatch.setattr(media_module, "_thumbnail_image", fake_thumb)

    result = media_module.extract_photo_slides(
        video_id=video_id,
        image_urls=["https://x/a.jpg", "https://x/b.jpg", "https://x/c.jpg"],
        is_important=False,
    )

    assert result.thumbs_created == 3
    assert result.full_frames_created == 0

    with session_scope() as s:
        rows = s.query(MediaArtifact).filter_by(video_id=video_id).all()
        assert all(r.kind == "slide_thumb" for r in rows)
        # Sequences match input order
        seqs = sorted([r.sequence for r in rows])
        assert seqs == [0, 1, 2]


def test_extract_photo_slides_full_res_when_important(fresh_db, monkeypatch):
    """When important: thumbs + full-res slides for every image."""
    tmp, video_id = fresh_db
    from media_archive.sources.tiktok.process import media as media_module
    from media_archive.core.db.schemas import MediaArtifact, session_scope

    def fake_download(url, dst, timeout=30):
        _make_jpeg(dst)
        return True

    monkeypatch.setattr(media_module, "_pillow_available", lambda: True)
    monkeypatch.setattr(media_module, "_download_image_url", fake_download)
    def fake_thumb(src, dst, width=256):
        import shutil
        shutil.copy(str(src), str(dst))
        return (256, 192)
    monkeypatch.setattr(media_module, "_thumbnail_image", fake_thumb)

    result = media_module.extract_photo_slides(
        video_id=video_id,
        image_urls=["https://x/a.jpg", "https://x/b.jpg"],
        is_important=True,
    )

    assert result.thumbs_created == 2
    assert result.full_frames_created == 2

    with session_scope() as s:
        rows = s.query(MediaArtifact).filter_by(video_id=video_id).all()
        kinds = sorted([r.kind for r in rows])
        assert kinds == ["slide_full", "slide_full", "slide_thumb", "slide_thumb"]
