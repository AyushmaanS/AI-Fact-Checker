import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional

import imageio_ffmpeg
from pydantic import BaseModel

from app.llm_client import MODEL_WHISPER, get_async_llm_client

logger = logging.getLogger(__name__)

AUDIO_EXTRACT_TIMEOUT = 60.0

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
