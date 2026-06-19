"""Tests for media_archive.core.transcribe.transcribe error classification (Phase 1.7.2).

The NoAudioStreamError type lets analyze_url skip the full stack trace
for an expected failure mode (yt-dlp returned an HTML page or malformed
download). Real Whisper / mlx execution is exercised manually.
"""
from __future__ import annotations

from unittest.mock import patch
import pytest


def test_no_audio_stream_error_is_runtime_error():
    """It should be catchable as a RuntimeError too, so callers that
    don't know about the new type still get reasonable behavior."""
    from media_archive.core.transcribe.transcribe import NoAudioStreamError
    e = NoAudioStreamError("test")
    assert isinstance(e, RuntimeError)


def test_extract_audio_classifies_no_stream_signature(tmp_path):
    """When ffmpeg's stderr contains the no-audio-stream signature,
    we raise NoAudioStreamError instead of a generic RuntimeError."""
    from media_archive.core.transcribe import transcribe

    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"not a video")

    class FakeProc:
        returncode = 1
        stderr = (
            "[out#0/wav @ 0x123] Output file does not contain any stream\n"
            "Error opening output file /tmp/audio.wav.\n"
        )
        stdout = ""

    with patch("media_archive.core.transcribe.transcribe.subprocess.run") as mock_run, \
         patch("media_archive.core.transcribe.transcribe.shutil.which", return_value="/opt/homebrew/bin/ffmpeg"):
        mock_run.return_value = FakeProc()
        with pytest.raises(transcribe.NoAudioStreamError):
            transcribe.extract_audio(fake_video, tmp_path / "out.wav")


def test_extract_audio_other_ffmpeg_failures_are_runtime_error(tmp_path):
    """Other ffmpeg failures (codec issues, file-not-found, etc.) keep
    the generic RuntimeError so analyze.py logs the full stack trace."""
    from media_archive.core.transcribe import transcribe

    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"not a video")

    class FakeProc:
        returncode = 1
        stderr = "Some unrelated ffmpeg complaint about codec X"
        stdout = ""

    with patch("media_archive.core.transcribe.transcribe.subprocess.run") as mock_run, \
         patch("media_archive.core.transcribe.transcribe.shutil.which", return_value="/opt/homebrew/bin/ffmpeg"):
        mock_run.return_value = FakeProc()
        with pytest.raises(RuntimeError) as exc_info:
            transcribe.extract_audio(fake_video, tmp_path / "out.wav")
        # Should NOT be NoAudioStreamError specifically
        assert not isinstance(exc_info.value, transcribe.NoAudioStreamError)


def test_extract_audio_classifies_invalid_data_signature(tmp_path):
    """yt-dlp sometimes hands us a partial download or HTML body. ffmpeg
    reports 'Invalid data found when processing input' for those, which
    we also classify as no-audio-stream (same root cause for our purposes)."""
    from media_archive.core.transcribe import transcribe

    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"<html>blocked</html>")

    class FakeProc:
        returncode = 1
        stderr = "Invalid data found when processing input"
        stdout = ""

    with patch("media_archive.core.transcribe.transcribe.subprocess.run") as mock_run, \
         patch("media_archive.core.transcribe.transcribe.shutil.which", return_value="/opt/homebrew/bin/ffmpeg"):
        mock_run.return_value = FakeProc()
        with pytest.raises(transcribe.NoAudioStreamError):
            transcribe.extract_audio(fake_video, tmp_path / "out.wav")
