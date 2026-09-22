import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import SUPABASE_KEY, SUPABASE_URL
from app.db import client

pytestmark = pytest.mark.skipif(
    not (SUPABASE_URL and SUPABASE_KEY),
    reason="SUPABASE_URL/SUPABASE_KEY not set in .env - see README for Supabase setup",
)


def test_insert_and_read_submission():
    submission = client.insert_submission(raw_input="dummy claim text", input_type="text")
    fetched = client.get_submission(submission["id"])
    assert fetched is not None
    assert fetched["raw_input"] == "dummy claim text"


def test_insert_and_read_claim():
    submission = client.insert_submission(raw_input="dummy claim text", input_type="text")
    claim = client.insert_claim(
        submission_id=submission["id"],
        text="The sky is blue.",
        topic="science",
        specificity="general",
        verifiability_score=0.9,
    )
    fetched = client.get_claim(claim["id"])
    assert fetched is not None
    assert fetched["text"] == "The sky is blue."


def test_insert_and_read_verdict():
    submission = client.insert_submission(raw_input="dummy claim text", input_type="text")
    claim = client.insert_claim(
        submission_id=submission["id"],
        text="The sky is blue.",
        topic="science",
        specificity="general",
        verifiability_score=0.9,
    )
    verdict = client.insert_verdict(
        claim_id=claim["id"],
        label="TRUE",
        rationale="Test rationale [SOURCE_1].",
        confidence_score=0.95,
        citations=["https://example.com/source"],
    )
    fetched = client.get_verdict(verdict["id"])
    assert fetched is not None
    assert fetched["label"] == "TRUE"


# --- Sprint 18: Supabase Storage + media/text retention ---


def test_update_submission_media_sets_storage_and_text_fields():
    submission = client.insert_submission(raw_input="dummy", input_type="upload")
    updated = client.update_submission_media(
        submission_id=submission["id"], storage_path="some/path.mp4", extracted_text="Caption: hello"
    )
    assert updated["storage_path"] == "some/path.mp4"
    assert updated["storage_expires_at"] is not None
    assert updated["extracted_text"] == "Caption: hello"
    assert updated["extracted_text_expires_at"] is not None


def test_update_submission_media_leaves_storage_fields_null_when_no_file():
    submission = client.insert_submission(raw_input="dummy", input_type="url")
    updated = client.update_submission_media(
        submission_id=submission["id"], storage_path=None, extracted_text="Caption: text-only fallback"
    )
    assert updated["storage_path"] is None
    assert updated["storage_expires_at"] is None
    assert updated["extracted_text"] == "Caption: text-only fallback"


def test_upload_and_delete_media_file(tmp_path):
    local_file = tmp_path / "test.txt"
    local_file.write_bytes(b"fake media bytes")
    storage_key = f"test-{uuid.uuid4()}.txt"

    client.upload_media_file(str(local_file), storage_key, "text/plain")
    client.delete_media_file(storage_key)  # shouldn't raise


def test_cleanup_expired_media_deletes_expired_files_and_clears_fields(tmp_path):
    local_file = tmp_path / "test.txt"
    local_file.write_bytes(b"fake media bytes")
    storage_key = f"cleanup-test-{uuid.uuid4()}.txt"
    client.upload_media_file(str(local_file), storage_key, "text/plain")

    submission = client.insert_submission(raw_input="dummy", input_type="upload")
    # Manually back-dated into the past to simulate an already-expired file,
    # rather than waiting 24 real hours for a genuine expiry.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    client.get_client().table("submissions").update(
        {"storage_path": storage_key, "storage_expires_at": past}
    ).eq("id", submission["id"]).execute()

    cleaned = client.cleanup_expired_media()
    assert cleaned >= 1

    fetched = client.get_submission(submission["id"])
    assert fetched["storage_path"] is None
    assert fetched["storage_expires_at"] is None
