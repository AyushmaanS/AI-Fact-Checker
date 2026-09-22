import asyncio
import mimetypes
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.agents.claim_extractor import extract_claims
from app.agents.intent_classifier import classify_intent
from app.agents.response_formatter import format_response, format_verdict_text
from app.db.client import (
    cleanup_expired_media,
    insert_claim,
    insert_submission,
    insert_verdict,
    update_submission_media,
    upload_media_file,
)
from app.ingestion.media_downloader import NO_VIDEO_ERROR, download_media
from app.ingestion.media_router import route_and_assemble
from app.ingestion.url_resolver import resolve_url
from app.models.schemas import StructuredContentObject, VerifyResponse
from app.pipeline import process_claims

router = APIRouter()

_INTENT_CANNED_MESSAGES = {
    "OPINION": "This reads as opinion, not a checkable claim.",
    "SATIRE_COMEDY": "This reads as satire or comedy, not a checkable claim.",
    "FICTIONAL_CREATIVE": "This reads as fictional or creative writing, not a checkable claim.",
    "UNRELATED": "This doesn't contain a factual claim to check.",
}

NO_CLAIMS_MESSAGE = "No verifiable claims found in the submitted text."
NO_EXTRACTABLE_CONTENT_MESSAGE = (
    "Ingestion completed, but no caption, transcript, or visual content could be "
    "extracted to check."
)
URL_DOWNLOAD_FAILED_MESSAGE = (
    "Could not download this content, and no caption text was available either - "
    "the post may be private, deleted, or the platform is rate-limiting downloads."
)
URL_NO_VIDEO_MESSAGE = (
    "This looks like a photo post, not a video - this app can currently only check "
    "Reels and other videos from a URL. Try pasting the caption text into the "
    "'Paste a claim' tab instead."
)


class VerifyRequest(BaseModel):
    text: str


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


async def _run_phase1_pipeline(submission_id: str, text: str, start: float) -> VerifyResponse:
    """Shared by all three /verify* routes once each has produced its own
    plain-text input (the raw submitted text, or an ingested media's combined
    transcript+visual_context+caption) - intent -> claims -> verdicts ->
    persistence -> response, identically regardless of where the text came from."""
    intent = await classify_intent(text)
    if intent.label != "FACTUAL_CLAIM":
        return format_response(
            submission_id=submission_id,
            claims=[],
            verdicts=[],
            processing_time_ms=_elapsed_ms(start),
            message=_INTENT_CANNED_MESSAGES.get(intent.label, "This doesn't contain a checkable factual claim."),
        )

    claims = await extract_claims(text)
    if not claims:
        return format_response(
            submission_id=submission_id,
            claims=[],
            verdicts=[],
            processing_time_ms=_elapsed_ms(start),
            message=NO_CLAIMS_MESSAGE,
        )

    # claim.claim_id ("c1", "c2", ...) is the pipeline's in-memory id, not the
    # claims table's generated uuid - verdicts.claim_id is a real FK to that uuid,
    # so persisted verdicts need this mapping, not the raw pipeline id.
    db_claim_id_by_pipeline_id: dict[str, str] = {}
    for claim in claims:
        row = await asyncio.to_thread(
            insert_claim,
            submission_id=submission_id,
            text=claim.text,
            topic=claim.topic,
            specificity=claim.specificity,
            verifiability_score=claim.verifiability_score,
        )
        db_claim_id_by_pipeline_id[claim.claim_id] = row["id"]

    verdicts = await process_claims(claims)

    await asyncio.gather(
        *(
            asyncio.to_thread(
                insert_verdict,
                claim_id=db_claim_id_by_pipeline_id[verdict.claim_id],
                label=verdict.label,
                rationale=format_verdict_text(verdict),
                confidence_score=verdict.confidence_score,
                citations=verdict.citations,
            )
            for verdict in verdicts
        )
    )

    return format_response(
        submission_id=submission_id,
        claims=claims,
        verdicts=verdicts,
        processing_time_ms=_elapsed_ms(start),
    )


@router.post("/verify", response_model=VerifyResponse)
async def verify(request: VerifyRequest) -> VerifyResponse:
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")

    start = time.perf_counter()
    submission = await asyncio.to_thread(insert_submission, raw_input=text, input_type="text")
    return await _run_phase1_pipeline(submission["id"], text, start)


def _combine_content_for_pipeline(content: StructuredContentObject) -> str:
    """Per Sprint 18's prompt: the combined transcript + visual_context +
    caption becomes the text input to the intent classifier/claim extractor -
    concatenated with clear section labels so the model can tell which part
    came from where, rather than one undifferentiated blob."""
    sections = []
    if content.caption:
        sections.append(f"Caption: {content.caption}")
    if content.transcript:
        sections.append(f"Transcript: {content.transcript}")
    if content.visual_context:
        sections.append(f"Visual context: {content.visual_context}")
    return "\n\n".join(sections)


def _guess_content_type(file_path: str) -> str:
    content_type, _ = mimetypes.guess_type(file_path)
    return content_type or "application/octet-stream"


async def _store_and_assemble(
    submission_id: str, file_path: Optional[str], caption: Optional[str], source_url: Optional[str]
) -> str:
    """Uploads the local media file (if any) to Supabase Storage, runs it
    through the media router, combines the result into pipeline-ready text,
    and persists both the storage location and the extracted text onto the
    submission row. Always cleans up the local file/temp dir afterward -
    Storage (or nothing, for a caption-only case) is the durable copy from
    here on, not the local disk. Returns the combined text (possibly empty)."""
    storage_path = None
    if file_path:
        storage_path = f"{submission_id}{Path(file_path).suffix}"
        await asyncio.to_thread(upload_media_file, file_path, storage_path, _guess_content_type(file_path))

    try:
        content = await route_and_assemble(file_path, caption=caption, source_url=source_url)
    finally:
        if file_path:
            shutil.rmtree(Path(file_path).parent, ignore_errors=True)

    combined_text = _combine_content_for_pipeline(content)
    await asyncio.to_thread(
        update_submission_media,
        submission_id=submission_id,
        storage_path=storage_path,
        extracted_text=combined_text,
    )
    return combined_text


async def _save_upload_to_temp(file: UploadFile) -> str:
    suffix = Path(file.filename).suffix if file.filename else ""
    temp_dir = tempfile.mkdtemp(prefix="upload_")
    temp_path = str(Path(temp_dir) / f"upload{suffix}")
    with open(temp_path, "wb") as f:
        f.write(await file.read())
    return temp_path


@router.post("/verify/upload", response_model=VerifyResponse)
async def verify_upload(file: UploadFile = File(...), caption: str = Form("")) -> VerifyResponse:
    await asyncio.to_thread(cleanup_expired_media)
    start = time.perf_counter()

    temp_path = await _save_upload_to_temp(file)
    submission = await asyncio.to_thread(insert_submission, raw_input=caption, input_type="upload")
    submission_id = submission["id"]

    combined_text = await _store_and_assemble(submission_id, temp_path, caption, source_url=None)
    if not combined_text.strip():
        return format_response(
            submission_id=submission_id,
            claims=[],
            verdicts=[],
            processing_time_ms=_elapsed_ms(start),
            message=NO_EXTRACTABLE_CONTENT_MESSAGE,
        )

    return await _run_phase1_pipeline(submission_id, combined_text, start)


class VerifyUrlRequest(BaseModel):
    url: str


@router.post("/verify/url", response_model=VerifyResponse)
async def verify_url(request: VerifyUrlRequest) -> VerifyResponse:
    raw_url = request.url.strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="url must not be empty")

    await asyncio.to_thread(cleanup_expired_media)
    start = time.perf_counter()

    resolved = await resolve_url(raw_url)
    result = await download_media(resolved.canonical_url)

    submission = await asyncio.to_thread(
        insert_submission, raw_input=resolved.canonical_url, input_type="url"
    )
    submission_id = submission["id"]

    if not result.success and not result.caption:
        # spec B.5: private/deleted/broken URL -> a clean user-facing message,
        # never a raw stack trace. Still a 200 - the request itself was handled
        # correctly, same as the non-factual-intent/zero-claim short circuits.
        message = URL_NO_VIDEO_MESSAGE if result.error == NO_VIDEO_ERROR else URL_DOWNLOAD_FAILED_MESSAGE
        return format_response(
            submission_id=submission_id,
            claims=[],
            verdicts=[],
            processing_time_ms=_elapsed_ms(start),
            message=message,
        )

    combined_text = await _store_and_assemble(
        submission_id, result.file_path, result.caption, source_url=resolved.canonical_url
    )
    if not combined_text.strip():
        return format_response(
            submission_id=submission_id,
            claims=[],
            verdicts=[],
            processing_time_ms=_elapsed_ms(start),
            message=NO_EXTRACTABLE_CONTENT_MESSAGE,
        )

    return await _run_phase1_pipeline(submission_id, combined_text, start)
