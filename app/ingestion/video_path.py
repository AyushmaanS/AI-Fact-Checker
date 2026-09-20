import asyncio
import base64
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import imageio_ffmpeg
from pydantic import BaseModel

from app.llm_client import MODEL_GPT4O, MODEL_WHISPER, get_async_llm_client

logger = logging.getLogger(__name__)

AUDIO_EXTRACT_TIMEOUT = 60.0
FRAME_EXTRACT_TIMEOUT = 60.0

# Sampled at 1fps (per the sprint prompt); a video longer than this many
# seconds gets evenly downsampled to roughly this many frames before being
# sent to GPT-4o Vision, to control cost - a short clip just sends every frame.
MAX_FRAMES_TO_SEND = 10

# Whisper's own commonly-used reference-decoder defaults, used together, not
# separately - confirmed live that no_speech_prob alone misses real
# hallucinations: a pure 440Hz tone got transcribed as "**BLEEP**" with
# no_speech_prob=0.44 (under a naive 0.5 threshold, so it would have looked
# "confident") but avg_logprob=-1.83 - Whisper's own token probabilities were
# decisively low. A real speech segment scored -0.30. avg_logprob catches what
# no_speech_prob misses here: not "was there sound," but "was Whisper actually
# sure about the words it produced."
NO_SPEECH_PROB_THRESHOLD = 0.6
AVG_LOGPROB_THRESHOLD = -1.0

# Fallback only, for the rare case Whisper returns segments at all but the
# no_speech_prob signal is otherwise unusable - typical speech runs ~10+
# chars/sec, so anything under 0.5 is very unlikely to be real speech.
MIN_CHARS_PER_SECOND = 0.5


class TranscriptionResult(BaseModel):
    transcript: str
    low_confidence_transcript: bool


def _extract_audio_sync(video_path: str) -> str:
    audio_path = str(Path(video_path).with_suffix("")) + "_audio.wav"
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ffmpeg_exe, "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", audio_path],
        capture_output=True,
        check=True,
        timeout=AUDIO_EXTRACT_TIMEOUT,
    )
    return audio_path


def _is_low_confidence(text: str, segments: list, duration: Optional[float]) -> bool:
    if segments:
        # Flag on ANY segment tripping either signal, not an average across
        # segments - a single hallucinated segment threaded into otherwise-real
        # speech is exactly the kind of fabricated "fact" this product cannot
        # afford to silently trust, so err toward flagging over averaging it away.
        return any(
            s.no_speech_prob > NO_SPEECH_PROB_THRESHOLD or s.avg_logprob < AVG_LOGPROB_THRESHOLD
            for s in segments
        )

    # No segments at all - e.g. pure music/silence, nothing detected. Per the
    # sprint prompt, fall back to transcript length relative to audio duration.
    if not duration or duration <= 0:
        return not text.strip()
    chars_per_second = len(text.strip()) / duration
    return chars_per_second < MIN_CHARS_PER_SECOND


async def transcribe_video(video_path: str) -> TranscriptionResult:
    """Extracts the audio track (ffmpeg) and transcribes it (Whisper). Never
    raises - a video with no meaningful speech (music-only, extraction
    failure, transcription failure) is a normal, expected outcome here,
    reported as an empty transcript with low_confidence_transcript=True
    rather than an exception, same stance as media_downloader/citation_verifier
    take on their own respective failure modes elsewhere in this codebase."""
    try:
        audio_path = await asyncio.to_thread(_extract_audio_sync, video_path)
    except Exception as exc:
        logger.info("video_path: audio extraction failed for %s (%s)", video_path, exc)
        return TranscriptionResult(transcript="", low_confidence_transcript=True)

    try:
        client = get_async_llm_client()
        with open(audio_path, "rb") as f:
            response = await client.audio.transcriptions.create(
                model=MODEL_WHISPER, file=f, response_format="verbose_json"
            )
    except Exception as exc:
        logger.info("video_path: transcription failed for %s (%s)", audio_path, exc)
        return TranscriptionResult(transcript="", low_confidence_transcript=True)
    finally:
        Path(audio_path).unlink(missing_ok=True)

    text = response.text or ""
    low_confidence = _is_low_confidence(text, response.segments or [], response.duration)
    return TranscriptionResult(transcript=text, low_confidence_transcript=low_confidence)


VISION_SYSTEM_PROMPT = (
    "You are shown one or more images - either frames sampled from a video, in "
    "chronological order, or a single static image. Do two things:\n"
    "1. description: describe the visual content across the frames as a "
    "whole - what's shown, what's happening.\n"
    "2. on_screen_text: transcribe any on-screen text you can read (captions, "
    "subtitles, overlaid statistics, labels, etc.), exactly as written. Leave "
    "this an empty string if there is no readable on-screen text in any frame - "
    "never invent text that isn't actually visible."
)


class _VisionAnalysis(BaseModel):
    description: str
    on_screen_text: str


def _extract_frames_sync(video_path: str) -> tuple[str, list[str]]:
    frame_dir = tempfile.mkdtemp(prefix="frames_")
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ffmpeg_exe, "-y", "-i", video_path, "-vf", "fps=1", "-q:v", "2", f"{frame_dir}/frame_%04d.jpg"],
        capture_output=True,
        check=True,
        timeout=FRAME_EXTRACT_TIMEOUT,
    )
    frame_paths = sorted(str(p) for p in Path(frame_dir).iterdir() if p.is_file())
    return frame_dir, frame_paths


def _select_frames(frame_paths: list[str]) -> list[str]:
    if len(frame_paths) <= MAX_FRAMES_TO_SEND:
        return frame_paths
    stride = max(1, len(frame_paths) // MAX_FRAMES_TO_SEND)
    return frame_paths[::stride][:MAX_FRAMES_TO_SEND]


def _encode_frame_as_data_url(frame_path: str) -> str:
    encoded = base64.b64encode(Path(frame_path).read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _combine_visual_context(description: str, on_screen_text: str) -> str:
    description = description.strip()
    on_screen_text = on_screen_text.strip()
    if not on_screen_text:
        return description
    return f"{description}\n\nOn-screen text: {on_screen_text}"


async def _analyze_frames_with_vision(frame_paths: list[str]) -> _VisionAnalysis:
    content = [{"type": "text", "text": "Image(s) to analyze, in order:"}]
    for path in frame_paths:
        content.append({"type": "image_url", "image_url": {"url": _encode_frame_as_data_url(path)}})

    completion = await get_async_llm_client().chat.completions.parse(
        model=MODEL_GPT4O,
        messages=[
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        response_format=_VisionAnalysis,
    )
    return completion.choices[0].message.parsed


async def analyze_frames(video_path: str) -> Optional[str]:
    """Extracts ~1fps frames (ffmpeg), sends a representative subset (capped at
    MAX_FRAMES_TO_SEND, evenly downsampled for longer videos) to GPT-4o Vision
    in one call, and combines its scene description with any on-screen text it
    transcribed into a single visual_context string. Never raises - a video
    whose frames can't be extracted or analyzed just contributes no
    visual_context (None), same graceful-degradation stance transcribe_video
    takes on audio."""
    try:
        frame_dir, frame_paths = await asyncio.to_thread(_extract_frames_sync, video_path)
    except Exception as exc:
        logger.info("video_path: frame extraction failed for %s (%s)", video_path, exc)
        return None

    if not frame_paths:
        shutil.rmtree(frame_dir, ignore_errors=True)
        return None

    try:
        analysis = await _analyze_frames_with_vision(_select_frames(frame_paths))
    except Exception as exc:
        logger.info("video_path: vision analysis failed for %s (%s)", video_path, exc)
        return None
    finally:
        shutil.rmtree(frame_dir, ignore_errors=True)

    return _combine_visual_context(analysis.description, analysis.on_screen_text)


async def analyze_image(image_path: str) -> Optional[str]:
    """Sends a single static image directly to GPT-4o Vision - no ffmpeg frame
    extraction needed, since there's only ever the one frame. Reuses the exact
    same vision call and combination logic as analyze_frames (spec has no
    separate "Image Path" component; a static image is just the video path's
    visual half applied to one image instead of several extracted frames).
    Never raises - an image that can't be analyzed just contributes no
    visual_context (None)."""
    try:
        analysis = await _analyze_frames_with_vision([image_path])
    except Exception as exc:
        logger.info("video_path: image analysis failed for %s (%s)", image_path, exc)
        return None
    return _combine_visual_context(analysis.description, analysis.on_screen_text)
