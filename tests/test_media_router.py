import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import imageio_ffmpeg
import pytest

from app.config import FASTROUTER_API_KEY
from app.ingestion.media_router import detect_media_type, route_and_assemble
from app.ingestion.video_path import TranscriptionResult

LIVE_SKIP = pytest.mark.skipif(not FASTROUTER_API_KEY, reason="FASTROUTER_API_KEY not set in .env")


# --- detect_media_type ---


def test_detect_media_type_video_extensions():
    assert detect_media_type("clip.mp4") == "video"
    assert detect_media_type("clip.mov") == "video"
    assert detect_media_type("clip.webm") == "video"


def test_detect_media_type_image_extensions():
    assert detect_media_type("photo.jpg") == "image"
    assert detect_media_type("photo.png") == "image"
    assert detect_media_type("photo.webp") == "image"


def test_detect_media_type_no_file_is_text_post():
    assert detect_media_type(None) == "text_post"
    assert detect_media_type("") == "text_post"


def test_detect_media_type_unrecognized_extension_falls_back_to_text_post():
    assert detect_media_type("mystery.xyz123") == "text_post"


# --- route_and_assemble: dispatch logic (mocked, fast, deterministic) ---


@patch("app.ingestion.media_router.analyze_frames")
@patch("app.ingestion.media_router.transcribe_video")
async def test_route_and_assemble_video_path(mock_transcribe, mock_analyze_frames):
    mock_transcribe.return_value = TranscriptionResult(
        transcript="The Eiffel Tower was completed in 1889.", low_confidence_transcript=False
    )
    mock_analyze_frames.return_value = "A tower against a blue sky."

    result = await route_and_assemble("clip.mp4", caption="Amazing! #paris", source_url="https://example.com/v")

    assert result.media_type == "video"
    assert result.transcript == "The Eiffel Tower was completed in 1889."
    assert result.low_confidence_transcript is False
    assert result.visual_context == "A tower against a blue sky."
    assert result.caption == "Amazing! #paris"
    assert result.topics == ["paris"]
    assert result.source_url == "https://example.com/v"
    mock_transcribe.assert_called_once_with("clip.mp4")
    mock_analyze_frames.assert_called_once_with("clip.mp4")


@patch("app.ingestion.media_router.analyze_image")
async def test_route_and_assemble_image_path(mock_analyze_image):
    mock_analyze_image.return_value = "A chart showing sales growth. On-screen text: UP 47%"

    result = await route_and_assemble("photo.jpg", caption="Look at this #stats")

    assert result.media_type == "image"
    assert result.transcript is None
    assert result.low_confidence_transcript is False
    assert result.visual_context == "A chart showing sales growth. On-screen text: UP 47%"
    assert result.caption == "Look at this #stats"
    assert result.topics == ["stats"]
    mock_analyze_image.assert_called_once_with("photo.jpg")


@patch("app.ingestion.media_router.analyze_image")
@patch("app.ingestion.media_router.analyze_frames")
@patch("app.ingestion.media_router.transcribe_video")
async def test_route_and_assemble_text_post_path_calls_no_media_analysis(
    mock_transcribe, mock_analyze_frames, mock_analyze_image
):
    result = await route_and_assemble(None, caption="Just a thought #musing")

    assert result.media_type == "text_post"
    assert result.transcript is None
    assert result.visual_context is None
    assert result.caption == "Just a thought #musing"
    assert result.topics == ["musing"]
    mock_transcribe.assert_not_called()
    mock_analyze_frames.assert_not_called()
    mock_analyze_image.assert_not_called()


async def test_route_and_assemble_handles_no_caption():
    result = await route_and_assemble(None, caption=None)
    assert result.caption == ""
    assert result.topics == []


# --- explicit schema-consistency check (real video, real image, real text -
# per the sprint prompt's own wording, run live, not mocked) ---


def _make_font_available(tmp_dir: str) -> str:
    # ffmpeg's drawtext filter chokes on a Windows drive-letter colon inside
    # the filter-argument string (e.g. "C:/Windows/Fonts/..."), so the font is
    # copied next to the output and referenced by bare filename with cwd set
    # to that directory instead. Windows-only font path - this whole project
    # is developed and run on Windows (see environment), so that's a fair
    # assumption for a test helper, just not portable beyond it.
    font_dst = str(Path(tmp_dir) / "arial.ttf")
    shutil.copy("C:/Windows/Fonts/arial.ttf", font_dst)
    return "arial.ttf"


def _generate_video_with_text(output_path: str, tmp_dir: str) -> None:
    font = _make_font_available(tmp_dir)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x240:d=3",
            "-vf",
            f"drawtext=fontfile={font}:text='TEST OVERLAY 99':fontcolor=white:"
            "fontsize=28:x=(w-text_w)/2:y=(h-text_h)/2",
            output_path,
        ],
        capture_output=True, check=True, cwd=tmp_dir, timeout=30,
    )


def _generate_image_with_text(output_path: str, tmp_dir: str) -> None:
    font = _make_font_available(tmp_dir)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=maroon:s=320x240:d=1",
            "-vf",
            f"drawtext=fontfile={font}:text='IMAGE OVERLAY 42':fontcolor=white:"
            "fontsize=28:x=(w-text_w)/2:y=(h-text_h)/2",
            "-frames:v", "1", output_path,
        ],
        capture_output=True, check=True, cwd=tmp_dir, timeout=30,
    )


@LIVE_SKIP
async def test_schema_consistency_across_all_three_media_types():
    with tempfile.TemporaryDirectory() as tmp_dir:
        video_path = str(Path(tmp_dir) / "test_video.mp4")
        image_path = str(Path(tmp_dir) / "test_image.jpg")
        _generate_video_with_text(video_path, tmp_dir)
        _generate_image_with_text(image_path, tmp_dir)

        video_obj, image_obj, text_obj = await asyncio.gather(
            route_and_assemble(video_path, caption="A video caption #test"),
            route_and_assemble(image_path, caption="An image caption #test"),
            route_and_assemble(None, caption="Just a caption, no media #test"),
        )

    # Same Pydantic model, same field set, in all three cases - the hard
    # interface contract spec B.5 calls out, even though some fields are null.
    assert (
        set(video_obj.model_dump().keys())
        == set(image_obj.model_dump().keys())
        == set(text_obj.model_dump().keys())
    )

    assert video_obj.media_type == "video"
    assert image_obj.media_type == "image"
    assert text_obj.media_type == "text_post"

    # Only a video ever gets a real transcript.
    assert video_obj.transcript is not None
    assert image_obj.transcript is None
    assert text_obj.transcript is None

    # Video and image both get visual_context; a caption-only post gets none.
    assert video_obj.visual_context is not None
    assert image_obj.visual_context is not None
    assert text_obj.visual_context is None

    # The on-screen text actually burned into each file should show up.
    assert "99" in video_obj.visual_context
    assert "42" in image_obj.visual_context

    # Caption/topics populate identically regardless of media type.
    assert video_obj.caption == "A video caption #test" and video_obj.topics == ["test"]
    assert image_obj.caption == "An image caption #test" and image_obj.topics == ["test"]
    assert text_obj.caption == "Just a caption, no media #test" and text_obj.topics == ["test"]
