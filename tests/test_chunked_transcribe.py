"""Tests for media_archive.core.transcribe.chunked (v0.2.0).

Whisper itself isn't tested here — we mock _transcribe_chunk_with_segments
and the underlying single-pass transcribe_video_file. The unit-under-test
is the chunking logic: deciding when to chunk, splitting audio with
ffmpeg, and merging chunk results back into a single timeline.
"""
from pathlib import Path
from unittest.mock import patch

from media_archive.core.transcribe.chunked import (
    CHUNK_DURATION_SEC,
    CHUNK_OVERLAP_SEC,
    SHORT_VIDEO_THRESHOLD_SEC,
    ChunkedTranscript,
    TranscriptSegment,
    _merge_chunks,
    chunked_transcribe_video_file,
)


# ---------------------------------------------------------------------------
# _merge_chunks: the core dedup logic
# ---------------------------------------------------------------------------

class TestMergeChunks:
    def test_single_chunk_passes_through(self):
        """One chunk in, all its segments out, timestamps unchanged."""
        segs = [
            TranscriptSegment(start=0.0, end=5.0, text="hello"),
            TranscriptSegment(start=5.0, end=12.0, text="world"),
        ]
        merged = _merge_chunks([(segs, 0.0)], overlap=30)
        assert len(merged) == 2
        assert merged[0].start == 0.0
        assert merged[1].text == "world"

    def test_offset_shifts_timestamps(self):
        """A chunk at offset=600 should produce segments with start += 600."""
        segs = [
            TranscriptSegment(start=0.0, end=5.0, text="alpha"),
            TranscriptSegment(start=5.0, end=10.0, text="beta"),
        ]
        merged = _merge_chunks([(segs, 600.0)], overlap=30)
        assert len(merged) == 2
        assert merged[0].start == 600.0
        assert merged[0].end == 605.0
        assert merged[1].start == 605.0

    def test_two_chunks_dedupe_overlap(self):
        """Segments in chunk 2 that fall within chunk 1's tail should be dropped.

        Setup: chunk 1 covers 0–1200 (offset 0), chunk 2 covers 1170–2370
        (offset 1170, since step = 1200 - 30 = 1170). The 30 sec overlap
        sits at 1170–1200. Chunk 2's first segment (start=0 in its local
        time, = 1170 absolute) should be dropped because chunk 1 already
        produced something at absolute time 1180.
        """
        chunk1_segs = [
            TranscriptSegment(start=0.0, end=10.0, text="A"),
            TranscriptSegment(start=1170.0, end=1180.0, text="overlap1"),
            TranscriptSegment(start=1180.0, end=1190.0, text="overlap2"),
        ]
        chunk2_segs = [
            # In chunk 2's local time, start=0 corresponds to abs=1170.
            # This is BEFORE chunk1's last_end of 1190, so should be dropped.
            TranscriptSegment(start=0.0, end=10.0, text="overlap1_again"),
            TranscriptSegment(start=10.0, end=20.0, text="overlap2_again"),
            # Chunk 2 segment at local time 30 = abs 1200, AFTER chunk1's
            # tail. This one keeps.
            TranscriptSegment(start=30.0, end=40.0, text="C"),
        ]
        merged = _merge_chunks(
            [(chunk1_segs, 0.0), (chunk2_segs, 1170.0)],
            overlap=30,
        )
        texts = [s.text for s in merged]
        # The two overlap-region segments from chunk2 should NOT appear.
        assert "overlap1_again" not in texts
        assert "overlap2_again" not in texts
        # The kept segments are A, overlap1, overlap2 (from chunk1) + C (from chunk2)
        assert texts == ["A", "overlap1", "overlap2", "C"]

    def test_three_chunks_progressive_dedupe(self):
        """A 3-chunk video. Each chunk's overlap with the prior gets dropped."""
        c1 = [TranscriptSegment(start=10.0, end=20.0, text="one")]
        c2 = [
            # offset 1170, local 0–10 = abs 1170–1180. After c1's last_end=20
            # so this stays.
            TranscriptSegment(start=0.0, end=10.0, text="two"),
            TranscriptSegment(start=1100.0, end=1150.0, text="two-tail"),
        ]
        c3 = [
            # offset 2340, local 0–10 = abs 2340–2350. After c2's last_end=2270.
            TranscriptSegment(start=0.0, end=10.0, text="three"),
        ]
        merged = _merge_chunks(
            [(c1, 0.0), (c2, 1170.0), (c3, 2340.0)],
            overlap=30,
        )
        texts = [s.text for s in merged]
        assert texts == ["one", "two", "two-tail", "three"]


# ---------------------------------------------------------------------------
# chunked_transcribe_video_file: the public entry point
# ---------------------------------------------------------------------------

class TestChunkedTranscribeVideoFile:
    def test_short_video_uses_single_pass(self, tmp_path):
        """For videos below SHORT_VIDEO_THRESHOLD_SEC, we delegate to the
        v1.x single-pass transcribe and wrap the result. No chunking,
        no extra ffmpeg calls."""
        fake_video = tmp_path / "short.mp4"
        fake_video.touch()  # exists check is the only real disk work

        with patch(
            "media_archive.core.transcribe.chunked.probe_duration",
            return_value=120.0,  # 2 minutes — well under threshold
        ), patch(
            "media_archive.core.transcribe.chunked.transcribe_video_file",
            return_value=("hello world", "en"),
        ) as mock_single_pass:
            result = chunked_transcribe_video_file(fake_video)

        assert isinstance(result, ChunkedTranscript)
        assert result.text == "hello world"
        assert result.language == "en"
        assert result.duration_sec == 120.0
        assert result.chunk_count == 1
        # Should have called the single-pass transcribe exactly once.
        assert mock_single_pass.call_count == 1

    def test_short_video_synthesizes_one_segment(self, tmp_path):
        """The short-video path wraps the transcript in a single segment
        spanning 0..duration so downstream UIs that expect segments
        always have something to render."""
        fake_video = tmp_path / "short.mp4"
        fake_video.touch()

        with patch(
            "media_archive.core.transcribe.chunked.probe_duration",
            return_value=300.0,
        ), patch(
            "media_archive.core.transcribe.chunked.transcribe_video_file",
            return_value=("the transcript text", "en"),
        ):
            result = chunked_transcribe_video_file(fake_video)

        assert len(result.segments) == 1
        assert result.segments[0].start == 0.0
        assert result.segments[0].end == 300.0
        assert result.segments[0].text == "the transcript text"

    def test_empty_short_transcript_yields_no_segments(self, tmp_path):
        """If the single-pass transcribe returns empty text (no speech),
        we shouldn't fabricate a fake segment."""
        fake_video = tmp_path / "silent.mp4"
        fake_video.touch()

        with patch(
            "media_archive.core.transcribe.chunked.probe_duration",
            return_value=60.0,
        ), patch(
            "media_archive.core.transcribe.chunked.transcribe_video_file",
            return_value=("", "en"),
        ):
            result = chunked_transcribe_video_file(fake_video)

        assert result.text == ""
        assert result.segments == []

    def test_unprobeable_video_falls_back_to_single_pass(self, tmp_path):
        """If ffprobe can't read duration, we shouldn't crash — we should
        fall back to the single-pass transcribe. This guards against
        malformed downloads where ffprobe fails but ffmpeg still extracts
        audio successfully."""
        fake_video = tmp_path / "weird.mp4"
        fake_video.touch()

        with patch(
            "media_archive.core.transcribe.chunked.probe_duration",
            return_value=0.0,  # 0 = probe failed
        ), patch(
            "media_archive.core.transcribe.chunked.transcribe_video_file",
            return_value=("recovered text", "en"),
        ) as mock_single_pass:
            result = chunked_transcribe_video_file(fake_video)

        assert mock_single_pass.call_count == 1
        assert result.text == "recovered text"
        assert result.duration_sec == 0.0
        assert result.chunk_count == 1


# ---------------------------------------------------------------------------
# Sanity: defaults
# ---------------------------------------------------------------------------

def test_default_thresholds_are_reasonable():
    """If someone tweaks the defaults, these are the constraints they
    must keep:
      - chunk_duration > overlap (otherwise step would be ≤ 0)
      - short_threshold ≥ chunk_duration (otherwise we'd chunk content
        that's smaller than a single chunk, which is silly)
    """
    assert CHUNK_DURATION_SEC > CHUNK_OVERLAP_SEC
    assert SHORT_VIDEO_THRESHOLD_SEC >= CHUNK_DURATION_SEC
