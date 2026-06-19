"""
Flask web UI.

Routes:
  GET  /                      home (analyze + recent videos)
  GET  /v/<id>                video detail (transcript, summary, Q&A)
  GET  /creators              creators list + last sync
  GET  /queue                 job queue dashboard
  POST /api/analyze           analyze a URL or uploaded file
  POST /api/ask/<id>          Q&A on a video
  GET  /api/health            health check
  GET  /api/queue             queue stats (polled every 5s by /queue page)
  POST /api/sync/<handle>     trigger a creator sync (enqueues job)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)
from sqlalchemy import desc, func

from media_archive.core import config
from media_archive.core.db.schemas import (
    Creator,
    Job,
    MediaArtifact,
    Video,
    get_session,
    init_db,
    session_scope,
)

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )

    # Limit upload size to 200 MB
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

    init_db()

    # ---- pages ----------------------------------------------------------

    @app.get("/")
    def home():
        session = get_session()
        try:
            recent = (
                session.query(Video)
                .order_by(desc(Video.created_at))
                .limit(20)
                .all()
            )
            recent_data = [_video_to_card(v) for v in recent]
        finally:
            session.close()
        return render_template("home.html", recent=recent_data, active_page="home")

    @app.get("/v/<int:video_id>")
    def detail(video_id: int):
        session = get_session()
        try:
            video = session.get(Video, video_id)
            if video is None:
                return render_template("not_found.html"), 404
            data = _video_to_full(video)
        finally:
            session.close()
        return render_template("detail.html", video=data, active_page="home")

    @app.get("/creators")
    def creators_page():
        session = get_session()
        try:
            creators = (
                session.query(Creator).order_by(Creator.handle.asc()).all()
            )
            counts = dict(
                session.query(Video.creator_id, func.count(Video.id))
                .group_by(Video.creator_id)
                .all()
            )
            data = [
                {
                    "id": c.id,
                    "handle": c.handle,
                    "display_name": c.display_name or c.handle,
                    "enabled": c.enabled,
                    "sync_depth": c.sync_depth,
                    "last_synced": c.last_synced_at.isoformat() if c.last_synced_at else None,
                    "video_count": counts.get(c.id, 0),
                    "sync_error": c.sync_error,
                    "notes": c.notes,
                }
                for c in creators
            ]
        finally:
            session.close()
        return render_template("creators.html", creators=data, active_page="creators")

    @app.get("/queue")
    def queue_page():
        return render_template("queue.html", active_page="queue")

    # ---- APIs -----------------------------------------------------------

    @app.get("/api/health")
    def health():
        from media_archive.sources.tiktok.process.tag import ollama_available
        ok, ollama_msg = ollama_available()
        return jsonify({
            "ok": True,
            "ollama": ollama_msg,
            "storage": config.STORAGE_BACKEND,
        })

    @app.get("/api/queue")
    def queue_api():
        from media_archive.core.queue import queue_stats
        stats = queue_stats()
        # Add latest 20 jobs for the dashboard
        session = get_session()
        try:
            recent = (
                session.query(Job)
                .order_by(desc(Job.created_at))
                .limit(20)
                .all()
            )
            recent_data = [
                {
                    "id": j.id,
                    "kind": j.kind,
                    "status": j.status,
                    "video_id": j.video_id,
                    "creator_id": j.creator_id,
                    "attempts": j.attempts,
                    "last_error": (j.last_error or "")[:200],
                    "scheduled_for": j.scheduled_for.isoformat() if j.scheduled_for else None,
                    "started_at": j.started_at.isoformat() if j.started_at else None,
                    "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                    "duration_sec": j.duration_sec,
                }
                for j in recent
            ]
        finally:
            session.close()
        return jsonify({"stats": stats, "recent": recent_data})

    @app.post("/api/analyze")
    def analyze_api():
        from media_archive.sources.tiktok.process.analyze import analyze_local_file, analyze_url

        url = (request.form.get("url") or "").strip()
        upload = request.files.get("file")

        if upload and upload.filename:
            scratch_path = config.SCRATCH_DIR / f"upload-{int(_now_ts())}-{upload.filename}"
            scratch_path.parent.mkdir(parents=True, exist_ok=True)
            upload.save(scratch_path)
            try:
                result = analyze_local_file(scratch_path)
            finally:
                # Apply same transcribe-and-discard policy
                try:
                    scratch_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return jsonify(result), (200 if result.get("ok") else 500)

        if url:
            result = analyze_url(url, keep_video=False)
            return jsonify(result), (200 if result.get("ok") else 500)

        return jsonify({"ok": False, "error": "Provide a URL or upload a file"}), 400

    @app.post("/api/ask/<int:video_id>")
    def ask_api(video_id: int):
        from media_archive.sources.tiktok.process.qa import ask
        body = request.get_json(silent=True) or {}
        question = (body.get("question") or "").strip()
        if not question:
            return jsonify({"ok": False, "error": "question is required"}), 400
        session = get_session()
        try:
            video = session.get(Video, video_id)
            if video is None:
                abort(404)
            if not video.transcript:
                return jsonify({
                    "ok": False,
                    "error": "Video has not been transcribed yet",
                }), 400
            transcript = video.transcript
        finally:
            session.close()
        answer = ask(transcript, question)
        return jsonify({"ok": True, "answer": answer})

    @app.post("/api/sync/<handle>")
    def trigger_sync_api(handle: str):
        """Enqueue a sync-creator job for one creator."""
        from media_archive.core.queue import enqueue
        from media_archive.sources.tiktok.sync import add_creator
        try:
            creator = add_creator(handle)
            enqueue("sync-creator", creator_id=creator.id)
            return jsonify({"ok": True, "creator_id": creator.id, "handle": creator.handle})
        except Exception as e:
            logger.exception("trigger_sync_api failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    # ---- Media serving and importance override (Phase 1.7) -----------

    @app.get("/media/<int:artifact_id>")
    def media_serve(artifact_id: int):
        """Serve a single MediaArtifact by id.

        Files live under MEDIA_DIR (outside the static dir) so they need
        an explicit endpoint. We do a basic path-jail check: the resolved
        path must live under MEDIA_DIR. Anything else 404s.
        """
        with session_scope() as s:
            row = s.get(MediaArtifact, artifact_id)
            if row is None or not row.local_path:
                abort(404)
            local_path = row.local_path
        path = Path(local_path).resolve()
        media_root = Path(config.MEDIA_DIR).resolve()
        try:
            path.relative_to(media_root)
        except ValueError:
            abort(404)
        if not path.is_file():
            abort(404)
        return send_file(str(path), mimetype="image/jpeg")

    @app.post("/api/importance/<int:video_id>")
    def importance_toggle_api(video_id: int):
        """Manually mark or unmark a video as important.

        Payload: {"important": true|false}

        Side effects:
        - Sets is_important and important_overridden=true so future
          re-analyses don't blow away the user's choice.
        - If becoming unimportant, drops all frame_full / slide_full
          artifacts (thumbnails stay).
        - If becoming important, returns immediately — re-running frame
          extraction here would block the request on ffmpeg + scenedetect.
          Caller should kick a re-extract job (future work).
        """
        body = request.get_json(silent=True) or {}
        target = bool(body.get("important", False))

        with session_scope() as s:
            v = s.get(Video, video_id)
            if v is None:
                return jsonify({"ok": False, "error": "video not found"}), 404
            previously_important = bool(v.is_important)
            v.is_important = target
            v.important_overridden = True
            v.importance_reason = "manual override via web UI"

        deleted = 0
        if previously_important and not target:
            # Demoting from important: clean up the full-res artifacts.
            from media_archive.sources.tiktok.process.media import drop_full_artifacts
            try:
                deleted = drop_full_artifacts(video_id)
            except Exception as e:
                logger.warning("Drop-full failed for video %d: %s", video_id, e)

        return jsonify({
            "ok": True,
            "video_id": video_id,
            "is_important": target,
            "deleted_full_artifacts": deleted,
        })

    return app


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _video_to_card(v: Video) -> dict:
    """Compact dict for list pages."""
    return {
        "id": v.id,
        "url": v.url,
        "author_handle": v.author_handle,
        "duration_sec": v.duration_sec,
        "transcript_lang": v.transcript_lang,
        "summary": (v.summary or "")[:200],
        "topics": _safe_json_list(v.topics),
        "intent": v.intent,
        "claim_check": v.claim_check,
        "transcribed": v.transcribed_at is not None,
        "tagged": v.tagged_at is not None,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "transcript_only": v.transcript_only,
    }


def _video_to_full(v: Video) -> dict:
    """Full dict for the detail page."""
    base = _video_to_card(v)
    base.update({
        "description": v.description,
        "transcript": v.transcript,
        "key_points": _safe_json_list(v.key_points),
        "view_count": v.view_count,
        "like_count": v.like_count,
        "thumbnail_url": v.thumbnail_url,
        # Phase 1.7: importance + media
        "post_type": getattr(v, "post_type", "video"),
        "is_important": bool(getattr(v, "is_important", False)),
        "importance_reason": getattr(v, "importance_reason", None),
        "important_overridden": bool(getattr(v, "important_overridden", False)),
        "media": _media_for_video(v.id),
    })
    return base


def _media_for_video(video_id: int) -> dict:
    """Return organized media artifact lists for the detail page.

    Returns a dict with keys: frame_thumbs, frame_fulls, slide_thumbs,
    slide_fulls. Each is a list of {id, sequence, timestamp_sec, url, w, h}.
    The url points at our /media/<id> endpoint, not the local path —
    the file lives outside the static dir.
    """
    out: dict[str, list[dict]] = {
        "frame_thumbs": [],
        "frame_fulls": [],
        "slide_thumbs": [],
        "slide_fulls": [],
    }
    with session_scope() as s:
        rows = (
            s.query(MediaArtifact)
            .filter_by(video_id=video_id)
            .order_by(MediaArtifact.kind, MediaArtifact.sequence)
            .all()
        )
        bucket_for = {
            "frame_thumb": "frame_thumbs",
            "frame_full":  "frame_fulls",
            "slide_thumb": "slide_thumbs",
            "slide_full":  "slide_fulls",
        }
        for r in rows:
            key = bucket_for.get(r.kind)
            if not key:
                continue
            out[key].append({
                "id": r.id,
                "sequence": r.sequence,
                "timestamp_sec": r.timestamp_sec,
                "url": f"/media/{r.id}",
                "w": r.width,
                "h": r.height,
            })
    return out


def _safe_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _now_ts() -> float:
    import time
    return time.time()
