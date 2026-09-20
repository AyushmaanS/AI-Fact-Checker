import asyncio
import mimetypes
from typing import Literal, Optional

from app.ingestion.caption_path import extract_caption_content
from app.ingestion.video_path import analyze_frames, analyze_image, transcribe_video
from app.models.schemas import StructuredContentObject

MediaType = Literal["video", "image", "text_post"]

# Python's mimetypes module doesn't register .webp by default on every
# platform (confirmed missing here) - registered explicitly since Instagram
# itself actively serves images as WebP.
mimetypes.add_type("image/webp", ".webp")


def detect_media_type(file_path: Optional[str]) -> MediaType:
    """No downloaded file at all -> text_post (caption-only input). Otherwise
    determined from the file's own MIME type (via its extension) - a
    downloaded file always has a real one, no guesswork needed. An
    unrecognized extension degrades to text_post rather than guessing at
    video/image handling for a format nothing here actually knows how to
    process - caption-only is always a safe fallback, never a crash."""
    if not file_path:
        return "text_post"
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type:
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("image/"):
            return "image"
    return "text_post"


async def route_and_assemble(
    file_path: Optional[str], caption: Optional[str] = None, source_url: Optional[str] = None
) -> StructuredContentObject:
    """Determines media_type from the downloaded file (if any) and dispatches
    to the Video Path (Sprints 15-16), a direct single-image analysis (spec
    has no separate "Image Path" - see video_path.analyze_image), or the
    Caption Path (Sprint 13) accordingly - assembling one StructuredContentObject
    regardless of which path ran. Fields the chosen path doesn't populate are
    left at their schema defaults (None/empty/False), not omitted, so every
    instance has the identical field shape spec B.5 calls out as the hard
    interface contract - Pydantic guarantees this structurally regardless, but
    the values are what actually differ correctly per media type: only a video
    ever gets a real transcript; only video/image ever get visual_context."""
    media_type = detect_media_type(file_path)
    caption_text, topics = extract_caption_content(caption or "")

    transcript: Optional[str] = None
    low_confidence_transcript = False
    visual_context: Optional[str] = None

    if media_type == "video":
        transcription, visual_context = await asyncio.gather(
            transcribe_video(file_path), analyze_frames(file_path)
        )
        transcript = transcription.transcript
        low_confidence_transcript = transcription.low_confidence_transcript
    elif media_type == "image":
        visual_context = await analyze_image(file_path)

    return StructuredContentObject(
        transcript=transcript,
        visual_context=visual_context,
        caption=caption_text,
        source_url=source_url,
        topics=topics,
        media_type=media_type,
        low_confidence_transcript=low_confidence_transcript,
    )
