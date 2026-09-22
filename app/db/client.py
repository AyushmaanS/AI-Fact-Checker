import logging
from datetime import datetime, timedelta, timezone

from supabase import Client, create_client

from app.config import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

MEDIA_BUCKET = "media"
STORAGE_RETENTION = timedelta(hours=24)
EXTRACTED_TEXT_RETENTION = timedelta(days=90)

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def insert_submission(raw_input: str, input_type: str) -> dict:
    result = get_client().table("submissions").insert(
        {"raw_input": raw_input, "input_type": input_type}
    ).execute()
    return result.data[0]


def get_submission(submission_id: str) -> dict | None:
    result = get_client().table("submissions").select("*").eq("id", submission_id).execute()
    return result.data[0] if result.data else None


def update_submission_media(
    submission_id: str,
    storage_path: str | None,
    extracted_text: str | None,
) -> dict:
    """Called after ingestion (download/upload + transcription/vision) completes
    for a Phase 2 submission, once there's something to record. storage_path is
    only set when a video file was actually stored (never for a caption-only
    submission); extracted_text is the combined transcript+visual_context+caption
    text that was fed into the Phase 1 pipeline. Each gets its own retention
    window from now, not from the submission's original created_at, since this
    update can happen a little after the row was first inserted."""
    now = datetime.now(timezone.utc)
    update = {}
    if storage_path is not None:
        update["storage_path"] = storage_path
        update["storage_expires_at"] = (now + STORAGE_RETENTION).isoformat()
    if extracted_text is not None:
        update["extracted_text"] = extracted_text
        update["extracted_text_expires_at"] = (now + EXTRACTED_TEXT_RETENTION).isoformat()

    result = get_client().table("submissions").update(update).eq("id", submission_id).execute()
    return result.data[0]


def upload_media_file(local_path: str, storage_key: str, content_type: str) -> None:
    with open(local_path, "rb") as f:
        get_client().storage.from_(MEDIA_BUCKET).upload(
            storage_key, f.read(), {"content-type": content_type}
        )


def delete_media_file(storage_key: str) -> None:
    get_client().storage.from_(MEDIA_BUCKET).remove([storage_key])


def cleanup_expired_media() -> int:
    """Deletes any stored media file whose storage_expires_at has passed, and
    clears the submission's storage fields (extracted_text is untouched - it
    has its own, much longer retention window). Implemented as a check run on
    each Phase 2 request rather than a scheduled job, per the sprint's own
    "a full cron job isn't required yet" allowance. Never raises - a single
    file's delete failing (e.g. already gone) shouldn't block the request that
    triggered this check."""
    now = datetime.now(timezone.utc).isoformat()
    expired = (
        get_client()
        .table("submissions")
        .select("id, storage_path")
        .lt("storage_expires_at", now)
        .not_.is_("storage_path", "null")
        .execute()
    )

    cleaned = 0
    for row in expired.data:
        try:
            delete_media_file(row["storage_path"])
        except Exception as exc:
            logger.warning("cleanup_expired_media: could not delete %s (%s)", row["storage_path"], exc)
        get_client().table("submissions").update(
            {"storage_path": None, "storage_expires_at": None}
        ).eq("id", row["id"]).execute()
        cleaned += 1
    return cleaned


def insert_claim(
    submission_id: str,
    text: str,
    topic: str,
    specificity: str,
    verifiability_score: float,
) -> dict:
    result = get_client().table("claims").insert(
        {
            "submission_id": submission_id,
            "text": text,
            "topic": topic,
            "specificity": specificity,
            "verifiability_score": verifiability_score,
        }
    ).execute()
    return result.data[0]


def get_claim(claim_id: str) -> dict | None:
    result = get_client().table("claims").select("*").eq("id", claim_id).execute()
    return result.data[0] if result.data else None


def insert_verdict(
    claim_id: str,
    label: str,
    rationale: str,
    confidence_score: float,
    citations: list[str],
) -> dict:
    result = get_client().table("verdicts").insert(
        {
            "claim_id": claim_id,
            "label": label,
            "rationale": rationale,
            "confidence_score": confidence_score,
            "citations": citations,
        }
    ).execute()
    return result.data[0]


def get_verdict(verdict_id: str) -> dict | None:
    result = get_client().table("verdicts").select("*").eq("id", verdict_id).execute()
    return result.data[0] if result.data else None


def list_source_credibility() -> list[dict]:
    result = get_client().table("source_credibility").select("*").execute()
    return result.data
