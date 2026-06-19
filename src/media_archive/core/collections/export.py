"""
Export a collection to a single markdown blob suitable for pasting into
a Claude (or other LLM) conversation.

Two verbosity modes:
  - compact (default): summary + key points + topics + intent per post,
    ~300 tokens per post. Good for collections of 50+ posts where the
    full transcripts would blow the context window.
  - full: everything compact has, plus the full transcript per post.
    Good for collections of <30 posts where you want the LLM to do
    deep analysis or quote specific passages.

The export also includes:
  - Header with collection name, description, member count, generation
    timestamp, and verbosity mode.
  - Per-post header with title/handle, URL, platform, duration, intent.
  - Footer telling the LLM what this is and offering interpretation hints.

The format choice (markdown) is deliberate. Both Claude and most other
LLMs handle markdown well, the human can preview the file before pasting,
and `cat` works fine for shell-piping use cases. JSON is available as a
secondary format for programmatic consumption.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def export_collection(
    collection_data: dict,
    *,
    format: str = "md",
    full_transcripts: bool = False,
    transcript_max_words: int = 0,  # 0 = no per-post truncation when full=True
) -> str:
    """Render a collection (as returned by ops.show_collection) to a string.

    `format`:
      - 'md'   — markdown, the primary use case
      - 'json' — structured JSON, useful for downstream programmatic use
      - 'txt'  — plain text, for old-school grep / less workflows

    `full_transcripts`: if False (default), include only summary +
    key_points + topics + intent + duration per post — roughly 300
    tokens per post. If True, include the full transcript (subject
    to transcript_max_words if > 0).

    `transcript_max_words`: only meaningful when full_transcripts=True.
    0 means "no truncation". Otherwise truncate each transcript to
    roughly N words (whitespace-split, suffixed with '… [truncated]').
    """
    if format == "json":
        return _export_json(collection_data, full_transcripts=full_transcripts)
    elif format == "txt":
        return _export_text(
            collection_data,
            full_transcripts=full_transcripts,
            transcript_max_words=transcript_max_words,
        )
    elif format == "md":
        return _export_markdown(
            collection_data,
            full_transcripts=full_transcripts,
            transcript_max_words=transcript_max_words,
        )
    else:
        raise ValueError(f"Unknown export format {format!r}. Use md, json, or txt.")


# ---------------------------------------------------------------------------
# Markdown writer (the main path)
# ---------------------------------------------------------------------------

def _export_markdown(
    coll: dict,
    *,
    full_transcripts: bool,
    transcript_max_words: int,
) -> str:
    """Generate the markdown export. Formatted for Claude paste-in.

    Structure:
        # Collection: <name>
        > <description if any>

        **Source:** media-archive collection export
        **Generated:** <UTC timestamp>
        **Verbosity:** compact|full
        **Member count:** N

        ---

        ## 1. <handle>: <truncated title> (<platform>)
        - URL: <url>
        - Duration: 4:23
        - Intent: <intent>
        - Topics: <comma-list>
        - Key points: <comma-list>

        Summary text here.

        [Transcript section if full_transcripts=True]

        ---
        [next post]
        ---

        ## How to use this archive

        <interpretation hints for the LLM>
    """
    parts: list[str] = []
    parts.append(_md_header(coll, full_transcripts=full_transcripts))
    parts.append("\n---\n")

    members = coll.get("members") or []
    if not members:
        parts.append("\n*This collection is empty.*\n")
    else:
        for i, m in enumerate(members, 1):
            parts.append(_md_member(
                m, index=i,
                full_transcripts=full_transcripts,
                transcript_max_words=transcript_max_words,
            ))
            parts.append("\n---\n")

    parts.append(_md_footer(coll, member_count=len(members)))
    return "".join(parts)


def _md_header(coll: dict, *, full_transcripts: bool) -> str:
    """The top-of-document header. Tells the LLM what it's looking at."""
    lines = []
    lines.append(f"# Collection: {coll.get('name', 'untitled')}\n")
    desc = coll.get("description")
    if desc:
        lines.append(f"\n> {desc}\n")
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    verbosity = "full transcripts" if full_transcripts else "compact (summaries only)"
    member_count = coll.get("member_count") or len(coll.get("members") or [])
    lines.append(
        f"\n**Source:** media-archive collection export\n"
        f"**Generated:** {now}\n"
        f"**Verbosity:** {verbosity}\n"
        f"**Members:** {member_count}\n"
    )
    return "".join(lines)


def _md_member(
    m: dict, *, index: int,
    full_transcripts: bool,
    transcript_max_words: int,
) -> str:
    """Render one collection member as a markdown section."""
    handle = m.get("author_handle") or "unknown"
    display = m.get("author_display_name")
    handle_display = f"{display} (@{handle})" if display and display != handle else f"@{handle}"

    title = (m.get("title") or "").strip().splitlines()[0] if m.get("title") else ""
    if len(title) > 80:
        title = title[:77] + "..."

    platform = m.get("platform") or "tiktok"
    duration = _format_duration(m.get("duration_sec"))

    lines = []
    title_line = f"## {index}. {handle_display}"
    if title:
        title_line += f" — {title}"
    title_line += f" ({platform})\n\n"
    lines.append(title_line)

    # Key facts as a tight bullet list
    bullets: list[str] = []
    bullets.append(f"- **URL:** {m.get('url', '')}")
    if duration:
        bullets.append(f"- **Duration:** {duration}")
    intent = m.get("intent")
    if intent:
        bullets.append(f"- **Intent:** {intent}")
    topics = m.get("topics") or []
    if topics:
        bullets.append(f"- **Topics:** {', '.join(str(t) for t in topics)}")
    key_points = m.get("key_points") or []
    if key_points:
        bullets.append(f"- **Key points:** {', '.join(str(k) for k in key_points)}")
    if m.get("claim_check"):
        bullets.append(f"- **Flagged for fact-check:** yes")
    note = m.get("note")
    if note:
        bullets.append(f"- **My note:** {note}")
    lines.append("\n".join(bullets) + "\n\n")

    summary = (m.get("summary") or "").strip()
    if summary:
        lines.append(f"{summary}\n\n")

    if full_transcripts:
        transcript = m.get("transcript") or ""
        if transcript:
            if transcript_max_words and transcript_max_words > 0:
                transcript = _truncate_words(transcript, transcript_max_words)
            lines.append("**Transcript:**\n\n")
            lines.append(f"> {_indent_blockquote(transcript)}\n\n")

    return "".join(lines)


def _md_footer(coll: dict, *, member_count: int) -> str:
    """Tail block telling the LLM how to interpret this archive."""
    name = coll.get("name", "this collection")
    return (
        "\n## About this archive\n\n"
        f"This is an export of {member_count} media post(s) "
        f"from a personal archive curated as the collection `{name}`. "
        "Each entry above is a real piece of content the user has analyzed and "
        "kept for reference. When answering questions, you can refer to entries "
        "by their number or by the URL/handle. If asked about something not "
        "covered in this collection, say so explicitly rather than speculating — "
        "the user has many other collections and may have the answer in one of them.\n"
    )


# ---------------------------------------------------------------------------
# Plain-text and JSON writers (secondary paths)
# ---------------------------------------------------------------------------

def _export_text(
    coll: dict, *, full_transcripts: bool, transcript_max_words: int,
) -> str:
    """Markdown without the formatting decorations. Useful for `less` /
    grep workflows where markdown syntax is just noise."""
    out = _export_markdown(
        coll,
        full_transcripts=full_transcripts,
        transcript_max_words=transcript_max_words,
    )
    # Strip the heaviest markdown markers
    return (
        out
        .replace("# ", "")
        .replace("## ", "")
        .replace("**", "")
        .replace("> ", "")
        .replace("---", "================================================================")
    )


def _export_json(coll: dict, *, full_transcripts: bool) -> str:
    """Structured JSON — full data without verbosity gating because
    consumers will filter themselves. We still respect full_transcripts
    by stripping `transcript` to None when False, since that's the
    bandwidth-hungry field."""
    payload = {
        "name": coll.get("name"),
        "description": coll.get("description"),
        "generated_at_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "verbosity": "full" if full_transcripts else "compact",
        "member_count": coll.get("member_count") or len(coll.get("members") or []),
        "members": [],
    }
    for m in (coll.get("members") or []):
        m_out = dict(m)
        # Datetimes don't serialize cleanly via json.dumps default. Coerce.
        for k, v in list(m_out.items()):
            if isinstance(v, _dt.datetime):
                m_out[k] = v.isoformat()
        if not full_transcripts:
            m_out.pop("transcript", None)
        payload["members"].append(m_out)
    return json.dumps(payload, indent=2, default=str)


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def _format_duration(seconds: Any) -> str:
    """Render seconds as 'M:SS' or 'H:MM:SS'. Returns '' if missing."""
    if seconds is None:
        return ""
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if s < 0:
        return ""
    hours, rem = divmod(s, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _truncate_words(text: str, max_words: int) -> str:
    """Coarse word-count truncation. Splits on whitespace, keeps first
    max_words tokens, and appends a marker so the LLM knows the cut
    happened."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " … [truncated]"


def _indent_blockquote(text: str) -> str:
    """Convert a multi-line transcript into a single blockquote by
    putting `> ` at the start of every continuation line. We do it
    this way (rather than per-line `> ` prefix in the caller) to
    keep the caller's template clean."""
    return text.strip().replace("\n", "\n> ")
