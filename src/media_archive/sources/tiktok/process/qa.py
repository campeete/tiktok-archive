"""
Per-video Q&A grounded in the transcript.

Hard-grounded: the prompt instructs the model to answer ONLY from the transcript
and to say so explicitly when the answer is not present. This avoids hallucination.
"""
from __future__ import annotations

import logging

import requests

from media_archive.core import config

logger = logging.getLogger(__name__)


_QA_PROMPT_TEMPLATE = """\
You are answering a question about a short-form video. You have ONLY the transcript below.

TRANSCRIPT:
\"\"\"
{transcript}
\"\"\"

QUESTION: {question}

Rules:
- Use ONLY information present in the transcript above.
- If the question asks about something not in the transcript, respond:
  "The transcript does not mention this."
- If the question is unrelated to the video's subject, respond:
  "This isn't covered in the video's content."
- Keep your answer to 1-3 sentences.
- Do not pad. Do not summarize the whole video. Just answer the question.

ANSWER:"""


def ask(transcript: str, question: str, *, model: str | None = None) -> str:
    """Ask a question grounded in the transcript. Returns the answer string."""
    transcript = (transcript or "").strip()
    question = (question or "").strip()
    if not transcript:
        return "No transcript is available for this video."
    if not question:
        return "Please provide a question."

    # Truncate transcript if needed; keep the start (where the hook usually is)
    max_chars = 8000
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars] + "\n[...truncated]"

    prompt = _QA_PROMPT_TEMPLATE.format(transcript=transcript, question=question)

    model = model or config.QA_MODEL
    url = f"{config.OLLAMA_HOST.rstrip('/')}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        answer = (data.get("response") or "").strip()
        return answer or "(no response)"
    except requests.RequestException as e:
        return f"Q&A failed: {e}"
