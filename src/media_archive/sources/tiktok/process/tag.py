"""
LLM-driven tagging: summary, key_points, topics, intent, claim_check.

Calls Ollama. Constrained outputs by:
- Controlled vocabulary (topics must come from tags_vocabulary.yaml)
- JSON-only response format
- Aggressive parsing with fallbacks
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests
import yaml

from media_archive.core import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vocabulary loading
# ---------------------------------------------------------------------------

_VOCABULARY_CACHE: dict | None = None


def load_vocabulary() -> dict:
    """Load the tag vocabulary, falling back to the example file if needed."""
    global _VOCABULARY_CACHE
    if _VOCABULARY_CACHE is not None:
        return _VOCABULARY_CACHE

    paths = [config.TAG_VOCABULARY_PATH, config.TAG_VOCABULARY_FALLBACK_PATH]
    for path in paths:
        if path and path.is_file():
            try:
                with path.open() as f:
                    data = yaml.safe_load(f) or {}
                if isinstance(data, dict):
                    _VOCABULARY_CACHE = data
                    return data
            except Exception as e:
                logger.warning("Failed to load %s: %s", path, e)

    # Empty vocabulary; LLM will produce free-form topics
    _VOCABULARY_CACHE = {"topics": [], "intents": []}
    return _VOCABULARY_CACHE


def vocabulary_topic_slugs() -> list[str]:
    vocab = load_vocabulary()
    topics = vocab.get("topics") or []
    return [t.get("slug") if isinstance(t, dict) else str(t) for t in topics]


def vocabulary_intents() -> list[str]:
    vocab = load_vocabulary()
    intents = vocab.get("intents") or [
        "educate", "entertain", "promote", "inform", "persuade", "vent", "other"
    ]
    return list(intents)


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------

def _ollama_generate(
    prompt: str, *, model: str | None = None, timeout: int = 120,
    response_format: dict | None = None,
) -> str:
    """Call Ollama /api/generate and return the response text."""
    model = model or config.TAG_MODEL
    url = f"{config.OLLAMA_HOST.rstrip('/')}/api/generate"
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    if response_format:
        payload["format"] = response_format
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return (data.get("response") or "").strip()
    except requests.HTTPError as e:
        raise RuntimeError(f"Ollama returned {e.response.status_code}: {e.response.text[:500]}") from e
    except requests.RequestException as e:
        raise RuntimeError(f"Ollama unreachable at {url}: {e}") from e


def ollama_available() -> tuple[bool, str]:
    """Check if Ollama is responding and the configured model is loaded."""
    url = f"{config.OLLAMA_HOST.rstrip('/')}/api/tags"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = [m.get("name") for m in data.get("models", [])]
        if config.TAG_MODEL not in models and not any(
            m.startswith(config.TAG_MODEL.split(":")[0]) for m in models
        ):
            return False, (
                f"Ollama up but model '{config.TAG_MODEL}' not found. "
                f"Run: ollama pull {config.TAG_MODEL}"
            )
        return True, "OK"
    except Exception as e:
        return False, f"Ollama not reachable at {config.OLLAMA_HOST}: {e}"


# ---------------------------------------------------------------------------
# Tagging prompt + parsing
# ---------------------------------------------------------------------------

_TAGGING_PROMPT_TEMPLATE = """\
You are analyzing a transcript from a short-form video (TikTok, Reels, etc.).

Produce a JSON object with EXACTLY these keys:
- "summary": one or two sentences capturing what the video is about.
- "key_points": array of 1-5 short bullet strings (no bullet markers, just text).
- "topics": array of topic slugs from this allowed list ONLY: {allowed_topics}
  If none fit, use an empty array. Pick 1-4 most relevant. NEVER invent new topics.
- "intent": one of: {allowed_intents}
- "claim_check": true if the transcript makes specific factual or numeric claims
  the viewer might want to verify, false otherwise.

TRANSCRIPT:
\"\"\"
{transcript}
\"\"\"

Respond with ONLY the JSON object. No prose before or after.
"""


def tag_transcript(
    transcript: str, *, model: str | None = None
) -> dict:
    """Run the tagging prompt and return a structured dict.

    On parse failure, returns a best-effort partial result instead of crashing.
    """
    transcript = (transcript or "").strip()
    if not transcript:
        return {
            "summary": "",
            "key_points": [],
            "topics": [],
            "intent": "other",
            "claim_check": False,
        }

    # Truncate very long transcripts to keep latency sane
    max_chars = 12000
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars] + "\n[...truncated]"

    allowed_topics = vocabulary_topic_slugs() or ["(any short snake_case slug)"]
    allowed_intents = vocabulary_intents()

    prompt = _TAGGING_PROMPT_TEMPLATE.format(
        allowed_topics=json.dumps(allowed_topics),
        allowed_intents=json.dumps(allowed_intents),
        transcript=transcript,
    )

    raw = _ollama_generate(prompt, model=model, response_format="json")
    return _parse_tag_response(raw, allowed_topics=allowed_topics, allowed_intents=allowed_intents)


def _parse_tag_response(raw: str, *, allowed_topics: list[str], allowed_intents: list[str]) -> dict:
    """Parse LLM response into our schema, robust to common deviations."""
    text = raw.strip()
    # Strip markdown code fence if the model added one
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON object inside the text
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                logger.warning("Failed to parse tag response: %s", text[:300])
                return {
                    "summary": text[:200],
                    "key_points": [],
                    "topics": [],
                    "intent": "other",
                    "claim_check": False,
                }
        else:
            return {
                "summary": text[:200],
                "key_points": [],
                "topics": [],
                "intent": "other",
                "claim_check": False,
            }

    # Coerce + validate
    summary = str(data.get("summary") or "").strip()
    raw_points = data.get("key_points") or []
    if not isinstance(raw_points, list):
        raw_points = [str(raw_points)]
    key_points = [str(p).strip().lstrip("-• ").strip() for p in raw_points if str(p).strip()][:5]

    raw_topics = data.get("topics") or []
    if not isinstance(raw_topics, list):
        raw_topics = [str(raw_topics)]
    topics = []
    allowed_set = set(allowed_topics) if allowed_topics else None
    for t in raw_topics:
        s = str(t).strip()
        if not s:
            continue
        if allowed_set is None or s in allowed_set:
            topics.append(s)

    intent = str(data.get("intent") or "other").strip().lower()
    if intent not in allowed_intents:
        intent = "other"

    raw_claim = data.get("claim_check")
    if isinstance(raw_claim, bool):
        claim_check = raw_claim
    elif isinstance(raw_claim, str):
        claim_check = raw_claim.strip().lower() in {"true", "yes", "1"}
    else:
        claim_check = False

    return {
        "summary": summary,
        "key_points": key_points,
        "topics": topics,
        "intent": intent,
        "claim_check": claim_check,
    }
