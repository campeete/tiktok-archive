"""
Importance judgment (Phase 1.7).

Given a transcript and summary, asks the LLM whether the visual content
of the post is essential to understanding the message. Used to decide
whether to keep full-resolution slides/frames or just thumbnails.

This is a separate, optional pass after tagging. It's deliberately
isolated:

- It can be re-run idempotently (same prompt → same answer, mostly).
- The human can override the result via the web UI.
- If Ollama is down, we fail to "not important" — this favors smaller
  storage over false-positive keeps. The cheap auto-rule (empty
  transcript → important) catches the most important case independently.

Cost: one ~5-15s qwen2.5:7b call per analyzed post. On a 700-post
overnight run, that's roughly an extra 1-3 hours. Cameron has accepted
this trade-off (see decision log D-027).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from media_archive.core import config
from media_archive.sources.tiktok.process.tag import _ollama_generate

logger = logging.getLogger(__name__)


# We give the model a tight envelope and a worked-out rubric. The model
# is much better at judging "important" if we tell it WHAT we mean by
# important — otherwise it'll lean toward "yes" because that's the safer
# answer in casual chat.
_IMPORTANCE_PROMPT = """\
You are evaluating whether the VISUAL content of a TikTok post is essential
to understanding what the post communicates.

A post's visuals are important when:
- The visuals contain text, charts, screenshots, or data that the spoken
  audio does NOT repeat.
- The post is showing-not-telling: a tutorial, a craft, a recipe, a
  before/after comparison, a demonstration.
- The visuals are the primary medium and the audio is secondary.

A post's visuals are NOT important when:
- It's a talking-head video where the speaker conveys the message in words.
- It's a meme, reaction, or commentary where the visual is decorative.
- The transcript already captures everything the post says.

Below is the post's transcript and short summary. Decide.

TRANSCRIPT:
\"\"\"{transcript}\"\"\"

SUMMARY:
\"\"\"{summary}\"\"\"

Respond ONLY with a JSON object: {{"important": true|false, "reason": "<one sentence>"}}
"""


@dataclass
class ImportanceJudgment:
    important: bool
    reason: str
    source: str  # "llm" | "auto-empty-transcript" | "creator-flag" | "manual"


def judge_importance(
    transcript: str | None,
    summary: str | None,
    *,
    creator_important: bool = False,
    timeout: int = 60,
) -> ImportanceJudgment:
    """Decide whether a post's visual content matters.

    Order of operations:
    1. If the creator is flagged important in creators.yaml, that wins.
    2. If the transcript is empty/whitespace, we auto-mark important —
       in that case the visual IS the message and we don't need the LLM
       to tell us so.
    3. Otherwise, ask the LLM. On any failure, return not-important.
    """
    if creator_important:
        return ImportanceJudgment(
            important=True,
            reason="Creator flagged as important in creators.yaml",
            source="creator-flag",
        )

    if not (transcript or "").strip():
        return ImportanceJudgment(
            important=True,
            reason="Empty transcript — visuals are the entire message",
            source="auto-empty-transcript",
        )

    # Trim aggressively. The judgment doesn't need the full transcript —
    # the first ~1500 chars + summary captures everything the model needs
    # and keeps the call fast.
    safe_transcript = (transcript or "")[:1500]
    safe_summary = (summary or "")[:500]
    prompt = _IMPORTANCE_PROMPT.format(
        transcript=safe_transcript, summary=safe_summary
    )

    try:
        raw = _ollama_generate(prompt, response_format="json", timeout=timeout)
    except Exception as e:
        logger.warning("Importance LLM call failed: %s; defaulting to not important", e)
        return ImportanceJudgment(
            important=False,
            reason=f"LLM unreachable: {e}",
            source="llm",
        )

    return _parse_importance_response(raw)


def _parse_importance_response(raw: str) -> ImportanceJudgment:
    """Be liberal in what we accept. The model sometimes wraps JSON in
    code fences, sometimes adds explanatory text. We just need true/false
    and a reason."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
        important = bool(data.get("important", False))
        reason = str(data.get("reason", ""))[:500] or "No reason given"
        return ImportanceJudgment(
            important=important, reason=reason, source="llm"
        )
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        logger.warning("Could not parse importance JSON: %s (raw=%r)", e, text[:200])
        # Default false — favor smaller storage over wrong keep
        return ImportanceJudgment(
            important=False,
            reason=f"Could not parse LLM response: {text[:100]}",
            source="llm",
        )
