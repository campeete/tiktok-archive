"""Tests for the importance judge.

Tests cover the auto-rules (creator flag, empty transcript) without
hitting Ollama, plus the parse logic for malformed LLM responses.
End-to-end LLM behavior is exercised on Cameron's Mac, not in CI.
"""
from __future__ import annotations


def test_creator_flag_short_circuits():
    """If creators.yaml flags a creator important, that beats everything.

    No LLM call, no auto-rule consultation — the creator flag is the
    user's explicit say-so, and we honor it without further analysis.
    """
    from media_archive.sources.tiktok.process.importance import judge_importance

    j = judge_importance(
        transcript="some normal talking-head transcript",
        summary="boring summary",
        creator_important=True,
    )
    assert j.important is True
    assert j.source == "creator-flag"
    assert "creators.yaml" in j.reason


def test_empty_transcript_auto_marks_important():
    """If the transcript is empty/whitespace, slides ARE the message.

    No LLM needed — the rule is deterministic and free.
    """
    from media_archive.sources.tiktok.process.importance import judge_importance

    for empty in ("", "   ", "\n\n\t", None):
        j = judge_importance(transcript=empty, summary="anything")
        assert j.important is True, f"empty={empty!r} should be important"
        assert j.source == "auto-empty-transcript"


def test_short_transcript_below_min_still_goes_to_llm(monkeypatch):
    """A 10-char transcript ("ok yeah um") is non-empty after stripping.
    We send it to the LLM rather than auto-marking — that's the LLM's job.
    """
    from media_archive.sources.tiktok.process import importance as imp

    called = {"n": 0}

    def fake_ollama(prompt, **kwargs):
        called["n"] += 1
        return '{"important": false, "reason": "talking head fragment"}'

    monkeypatch.setattr(imp, "_ollama_generate", fake_ollama)

    j = imp.judge_importance(transcript="ok yeah um", summary="brief clip")
    assert called["n"] == 1
    assert j.important is False
    assert j.source == "llm"


def test_llm_response_parsed_when_marked_important(monkeypatch):
    """Happy path: LLM returns clean JSON saying important=true."""
    from media_archive.sources.tiktok.process import importance as imp

    monkeypatch.setattr(
        imp,
        "_ollama_generate",
        lambda prompt, **kw: '{"important": true, "reason": "tutorial with on-screen code"}',
    )
    j = imp.judge_importance(
        transcript="this is a long transcript " * 20,
        summary="coding tutorial",
    )
    assert j.important is True
    assert j.source == "llm"
    assert "tutorial" in j.reason


def test_llm_response_with_code_fence_still_parses(monkeypatch):
    """qwen2.5 sometimes wraps JSON in ```json fences. Parser handles it."""
    from media_archive.sources.tiktok.process import importance as imp

    monkeypatch.setattr(
        imp,
        "_ollama_generate",
        lambda prompt, **kw: '```json\n{"important": true, "reason": "data viz"}\n```',
    )
    j = imp.judge_importance(
        transcript="long transcript " * 20, summary="charts and figures"
    )
    assert j.important is True


def test_llm_failure_defaults_to_not_important(monkeypatch):
    """If Ollama is unreachable, we MUST default to not-important.

    The alternative (defaulting to important) would mean storage blowing
    up whenever Ollama is flaky. Conservative default favors smaller
    storage; the empty-transcript auto-rule already catches the
    must-keep case.
    """
    from media_archive.sources.tiktok.process import importance as imp

    def boom(prompt, **kw):
        raise RuntimeError("ollama unreachable")
    monkeypatch.setattr(imp, "_ollama_generate", boom)

    j = imp.judge_importance(
        transcript="some transcript " * 20, summary="something"
    )
    assert j.important is False
    assert "unreachable" in j.reason or "error" in j.reason.lower()


def test_malformed_json_response_defaults_to_not_important(monkeypatch):
    """LLM hallucinates a non-JSON answer. Don't crash — default false."""
    from media_archive.sources.tiktok.process import importance as imp

    monkeypatch.setattr(
        imp,
        "_ollama_generate",
        lambda prompt, **kw: "Yes, this is important because lorem ipsum",
    )
    j = imp.judge_importance(
        transcript="long transcript " * 20, summary="anything"
    )
    assert j.important is False


def test_creator_flag_beats_empty_transcript():
    """If both apply (creator flagged AND transcript empty), the creator
    flag wins. Both produce the same answer (important=true) but the
    `source` field should reflect creator-flag for accurate provenance."""
    from media_archive.sources.tiktok.process.importance import judge_importance

    j = judge_importance(transcript="", summary="", creator_important=True)
    assert j.important is True
    assert j.source == "creator-flag"
