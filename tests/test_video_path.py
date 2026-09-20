import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.ingestion.video_path import (
    MAX_FRAMES_TO_SEND,
    _combine_visual_context,
    _encode_frame_as_data_url,
    _is_low_confidence,
    _select_frames,
    _VisionAnalysis,
    analyze_frames,
    transcribe_video,
)


def _segment(no_speech_prob: float, avg_logprob: float = -0.3, text: str = "some text") -> SimpleNamespace:
    # -0.3 as the default mirrors a real confident-speech segment observed live.
    return SimpleNamespace(no_speech_prob=no_speech_prob, avg_logprob=avg_logprob, text=text)


def _whisper_response(text: str, segments: list, duration: float) -> MagicMock:
    response = MagicMock()
    response.text = text
    response.segments = segments
    response.duration = duration
    return response


def _fake_audio_file() -> str:
    # A real (empty) file, not just a path string - transcribe_video genuinely
    # opens this path with open(..., "rb"), so it has to exist on disk.
    fd, path = tempfile.mkstemp(suffix=".wav")
    import os

    os.close(fd)
    return path


# --- _is_low_confidence ---


def test_is_low_confidence_false_for_real_speech_segments():
    segments = [_segment(0.001), _segment(0.02), _segment(0.005)]
    assert _is_low_confidence("The Eiffel Tower was completed in 1889.", segments, 5.4) is False


def test_is_low_confidence_true_when_segments_all_high_no_speech_prob():
    segments = [_segment(0.9), _segment(0.85)]
    assert _is_low_confidence("um... uh...", segments, 5.0) is True


def test_is_low_confidence_true_for_low_avg_logprob_even_with_borderline_no_speech_prob():
    # Reproduces a real live finding: a pure 440Hz tone got hallucinated into
    # "**BLEEP**" with no_speech_prob=0.44 - under a naive 0.5 threshold, so it
    # would have looked "confident" - but avg_logprob=-1.83, meaning Whisper's
    # own token probabilities were actually very low. A real speech segment
    # scored -0.30. no_speech_prob alone is not enough to catch this.
    segments = [_segment(no_speech_prob=0.44, avg_logprob=-1.83, text="**BLEEP**")]
    assert _is_low_confidence("**BLEEP**", segments, 5.0) is True


def test_is_low_confidence_flags_if_any_single_segment_is_bad_not_just_the_average():
    # One hallucinated segment threaded among otherwise-confident ones must
    # still trip the flag - averaging it away would silently trust a fabricated
    # detail in an otherwise-real transcript.
    segments = [_segment(0.01, -0.3), _segment(0.01, -0.3), _segment(0.5, -1.5)]
    assert _is_low_confidence("mostly fine text here", segments, 10.0) is True


def test_is_low_confidence_true_when_no_segments_and_empty_text():
    # A pure-tone/music clip: Whisper returns no segments and an empty
    # transcript - the exact behavior confirmed live for a silent/music input.
    assert _is_low_confidence("", [], 5.0) is True


def test_is_low_confidence_false_when_no_segments_but_dense_text():
    # No segments available (verbose_json's own signal unusable), but the
    # length-based fallback still finds a real amount of text - not low confidence.
    long_text = "a real word " * 20
    assert _is_low_confidence(long_text, [], 5.0) is False


def test_is_low_confidence_handles_zero_duration_without_crashing():
    assert _is_low_confidence("", [], 0.0) is True
    assert _is_low_confidence("", [], None) is True


# --- transcribe_video ---


@patch("app.ingestion.video_path.get_async_llm_client")
@patch("app.ingestion.video_path._extract_audio_sync")
async def test_transcribe_video_success_with_real_speech(mock_extract, mock_get_client):
    audio_path = _fake_audio_file()
    mock_extract.return_value = audio_path
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = AsyncMock(
        return_value=_whisper_response(
            "The Eiffel Tower was completed in 1889.", [_segment(0.001)], 5.4
        )
    )
    mock_get_client.return_value = mock_client

    result = await transcribe_video("/tmp/fake_video.mp4")

    assert result.transcript == "The Eiffel Tower was completed in 1889."
    assert result.low_confidence_transcript is False


@patch("app.ingestion.video_path.get_async_llm_client")
@patch("app.ingestion.video_path._extract_audio_sync")
async def test_transcribe_video_music_only_sets_low_confidence_not_hallucinated_text(
    mock_extract, mock_get_client
):
    audio_path = _fake_audio_file()
    mock_extract.return_value = audio_path
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = AsyncMock(
        return_value=_whisper_response("", [], 5.0)
    )
    mock_get_client.return_value = mock_client

    result = await transcribe_video("/tmp/fake_video.mp4")

    assert result.transcript == ""
    assert result.low_confidence_transcript is True


@patch("app.ingestion.video_path._extract_audio_sync")
async def test_transcribe_video_handles_extraction_failure_gracefully(mock_extract):
    mock_extract.side_effect = RuntimeError("ffmpeg exploded")

    result = await transcribe_video("/tmp/fake_video.mp4")

    assert result.transcript == ""
    assert result.low_confidence_transcript is True


@patch("app.ingestion.video_path.get_async_llm_client")
@patch("app.ingestion.video_path._extract_audio_sync")
async def test_transcribe_video_handles_api_failure_gracefully(mock_extract, mock_get_client):
    audio_path = _fake_audio_file()
    mock_extract.return_value = audio_path
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = AsyncMock(side_effect=RuntimeError("API down"))
    mock_get_client.return_value = mock_client

    result = await transcribe_video("/tmp/fake_video.mp4")

    assert result.transcript == ""
    assert result.low_confidence_transcript is True
    # Even on failure, the extracted audio file must still be cleaned up.
    assert not Path(audio_path).exists()


@patch("app.ingestion.video_path.get_async_llm_client")
@patch("app.ingestion.video_path._extract_audio_sync")
async def test_transcribe_video_cleans_up_extracted_audio_file(mock_extract, mock_get_client):
    audio_path = _fake_audio_file()
    mock_extract.return_value = audio_path
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = AsyncMock(
        return_value=_whisper_response("hi", [_segment(0.01)], 1.0)
    )
    mock_get_client.return_value = mock_client

    assert Path(audio_path).exists()
    await transcribe_video("/tmp/fake_video.mp4")
    assert not Path(audio_path).exists()


# --- Sprint 16: frame sampling + GPT-4o Vision ---


def _fake_frame_dir(num_frames: int) -> tuple[str, list[str]]:
    frame_dir = tempfile.mkdtemp(prefix="test_frames_")
    paths = []
    for i in range(num_frames):
        path = os.path.join(frame_dir, f"frame_{i:04d}.jpg")
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fake jpeg bytes")  # real JPEG magic bytes, fake payload
        paths.append(path)
    return frame_dir, paths


def _vision_analysis(description: str, on_screen_text: str = "") -> _VisionAnalysis:
    return _VisionAnalysis(description=description, on_screen_text=on_screen_text)


# --- _select_frames ---


def test_select_frames_returns_all_when_under_cap():
    paths = [f"frame_{i}.jpg" for i in range(5)]
    assert _select_frames(paths) == paths


def test_select_frames_returns_all_when_exactly_at_cap():
    paths = [f"frame_{i}.jpg" for i in range(MAX_FRAMES_TO_SEND)]
    assert _select_frames(paths) == paths


def test_select_frames_downsamples_evenly_when_over_cap():
    paths = [f"frame_{i}.jpg" for i in range(97)]  # a long ~97-second video
    selected = _select_frames(paths)
    assert len(selected) <= MAX_FRAMES_TO_SEND
    assert len(selected) > 0
    # Order is preserved and it's an even stride, not just the first N frames.
    assert selected == sorted(selected, key=paths.index)
    assert selected[-1] != paths[MAX_FRAMES_TO_SEND - 1]  # actually strided, not truncated


# --- _combine_visual_context ---


def test_combine_visual_context_with_on_screen_text():
    result = _combine_visual_context("A person at a podium.", "SALES UP 47 PERCENT")
    assert result == "A person at a podium.\n\nOn-screen text: SALES UP 47 PERCENT"


def test_combine_visual_context_without_on_screen_text():
    result = _combine_visual_context("A quiet street scene.", "")
    assert result == "A quiet street scene."


def test_combine_visual_context_strips_whitespace():
    result = _combine_visual_context("  Some description.  ", "   ")
    assert result == "Some description."


# --- _encode_frame_as_data_url ---


def test_encode_frame_as_data_url_produces_valid_data_url():
    frame_dir, paths = _fake_frame_dir(1)
    try:
        url = _encode_frame_as_data_url(paths[0])
        assert url.startswith("data:image/jpeg;base64,")
    finally:
        for p in paths:
            os.unlink(p)
        os.rmdir(frame_dir)


# --- analyze_frames ---


@patch("app.ingestion.video_path._analyze_frames_with_vision")
@patch("app.ingestion.video_path._extract_frames_sync")
async def test_analyze_frames_success(mock_extract, mock_analyze):
    frame_dir, paths = _fake_frame_dir(3)
    mock_extract.return_value = (frame_dir, paths)
    mock_analyze.return_value = _vision_analysis("A stat overlay on a blue background.", "SALES UP 47 PERCENT")

    result = await analyze_frames("/tmp/fake_video.mp4")

    assert result == "A stat overlay on a blue background.\n\nOn-screen text: SALES UP 47 PERCENT"
    # Frames must be cleaned up after a successful analysis too.
    assert not Path(frame_dir).exists()


@patch("app.ingestion.video_path._extract_frames_sync")
async def test_analyze_frames_handles_extraction_failure_gracefully(mock_extract):
    mock_extract.side_effect = RuntimeError("ffmpeg exploded")
    assert await analyze_frames("/tmp/fake_video.mp4") is None


@patch("app.ingestion.video_path._extract_frames_sync")
async def test_analyze_frames_handles_zero_frames_extracted(mock_extract):
    frame_dir = tempfile.mkdtemp(prefix="test_frames_empty_")
    mock_extract.return_value = (frame_dir, [])

    assert await analyze_frames("/tmp/fake_video.mp4") is None
    assert not Path(frame_dir).exists()


@patch("app.ingestion.video_path._analyze_frames_with_vision")
@patch("app.ingestion.video_path._extract_frames_sync")
async def test_analyze_frames_handles_vision_failure_gracefully(mock_extract, mock_analyze):
    frame_dir, paths = _fake_frame_dir(2)
    mock_extract.return_value = (frame_dir, paths)
    mock_analyze.side_effect = RuntimeError("vision API down")

    result = await analyze_frames("/tmp/fake_video.mp4")

    assert result is None
    # Even on failure, extracted frames must still be cleaned up.
    assert not Path(frame_dir).exists()


@patch("app.ingestion.video_path._analyze_frames_with_vision")
@patch("app.ingestion.video_path._extract_frames_sync")
async def test_analyze_frames_passes_only_selected_frames_to_vision(mock_extract, mock_analyze):
    frame_dir, paths = _fake_frame_dir(50)
    mock_extract.return_value = (frame_dir, paths)
    mock_analyze.return_value = _vision_analysis("Some scene.")

    await analyze_frames("/tmp/fake_video.mp4")

    sent_frames = mock_analyze.call_args.args[0]
    assert len(sent_frames) <= MAX_FRAMES_TO_SEND
