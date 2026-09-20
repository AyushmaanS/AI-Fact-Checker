import asyncio
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.agents.claim_extractor import extract_claims
from app.agents.intent_classifier import classify_intent
from app.agents.response_formatter import format_response, format_verdict_text
from app.db.client import insert_claim, insert_submission, insert_verdict
from app.ingestion.caption_path import extract_caption_content
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

# Sprint 14's downloader/transcription pipeline doesn't exist yet, so an upload
# is ingested (saved + wrapped into a StructuredContentObject) but not yet run
# through fact-checking - this says so rather than silently returning an empty
# verdict list with no explanation.
UPLOAD_PLACEHOLDER_MESSAGE = (
    "File received and saved. Fact-checking on uploaded video isn't wired up yet "
    "- transcription and vision analysis are a later sprint."
)


class VerifyRequest(BaseModel):
    text: str


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


@router.post("/verify", response_model=VerifyResponse)
async def verify(request: VerifyRequest) -> VerifyResponse:
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")

    start = time.perf_counter()

    submission = await asyncio.to_thread(insert_submission, raw_input=text, input_type="text")
    submission_id = submission["id"]

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


async def _save_upload_to_temp(file: UploadFile) -> str:
    suffix = Path(file.filename).suffix if file.filename else ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        return tmp.name


def _build_upload_content(caption: str) -> StructuredContentObject:
    caption_text, topics = extract_caption_content(caption)
    return StructuredContentObject(
        caption=caption_text,
        topics=topics,
        media_type="video",
        transcript=None,  # placeholder - no downloader/transcription until Sprint 14+
    )


@router.post("/verify/upload", response_model=VerifyResponse)
async def verify_upload(file: UploadFile = File(...), caption: str = Form("")) -> VerifyResponse:
    start = time.perf_counter()

    # Confirms the upload is durably saved even though nothing consumes it yet -
    # Sprint 14's downloader will read from a saved path like this one.
    await _save_upload_to_temp(file)
    content = _build_upload_content(caption)

    submission = await asyncio.to_thread(
        insert_submission, raw_input=content.caption, input_type="video_upload"
    )

    return format_response(
        submission_id=submission["id"],
        claims=[],
        verdicts=[],
        processing_time_ms=_elapsed_ms(start),
        message=UPLOAD_PLACEHOLDER_MESSAGE,
    )
