"""media-archive: local-first multi-source media analyzer.

This package is the v2 evolution of tiktok-archive (v1.7.x). The v1.x
project handled a single source (TikTok); v2 generalizes to multiple
sources via the `media_archive.sources.*` plugin layout, adds long-form
transcription support (segments + jump-to-timestamp), and exposes the
underlying functions through both an HTTP/JSON API and an MCP server
under `media_archive.api`.

Layout:
    media_archive.core      shared infrastructure (db, queue, config, storage, webapp shell)
    media_archive.sources   per-source plugins (tiktok/, youtube/, instagram/, ...)
    media_archive.api       HTTP/JSON server + MCP server (both sit on top of core)
    media_archive.cli       top-level dispatcher; routes commands to the right source
"""

__version__ = "0.3.0"
