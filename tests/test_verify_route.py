import io
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.agents.verdict_agent import NO_SOURCES_RATIONALE
from app.config import FASTROUTER_API_KEY, SUPABASE_KEY, SUPABASE_URL, TAVILY_API_KEY
from app.ingestion.media_downloader import PHOTO_POST_FALLBACK_FAILED_ERROR, DownloadResult
from app.ingestion.url_resolver import ResolvedURL
from app.main import app
from app.models.schemas import Claim, ContentIntent, EvidenceLine, StructuredContentObject, Verdict
from app.routes.verify import (
    NO_EXTRACTABLE_CONTENT_MESSAGE,
    URL_DOWNLOAD_FAILED_MESSAGE,
    URL_PHOTO_POST_FALLBACK_FAILED_MESSAGE,
    _combine_content_for_pipeline,
    _save_upload_to_temp,
)

client = TestClient(app)

LIVE_SKIP = pytest.mark.skipif(
    not (TAVILY_API_KEY and FASTROUTER_API_KEY and SUPABASE_URL and SUPABASE_KEY),
    reason="TAVILY_API_KEY / FASTROUTER_API_KEY / SUPABASE_URL / SUPABASE_KEY not set in .env",
)


def test_empty_text_returns_400():
    response = client.post("/verify", json={"text": "   "})
    assert response.status_code == 400


@patch("app.routes.verify.insert_submission")
@patch("app.routes.verify.classify_intent")
def test_non_factual_intent_short_circuits(mock_classify, mock_insert_submission):
    mock_insert_submission.return_value = {"id": "sub-1"}
    mock_classify.return_value = ContentIntent(label="OPINION", confidence=0.9)

    response = client.post("/verify", json={"text": "Pineapple belongs on pizza."})

    assert response.status_code == 200
    body = response.json()
    assert body["claims"] == []
    assert body["verdicts"] == []
    assert "opinion" in body["message"].lower()


@patch("app.routes.verify.insert_submission")
@patch("app.routes.verify.classify_intent")
@patch("app.routes.verify.extract_claims")
def test_zero_claims_short_circuits(mock_extract, mock_classify, mock_insert_submission):
    mock_insert_submission.return_value = {"id": "sub-1"}
    mock_classify.return_value = ContentIntent(label="FACTUAL_CLAIM", confidence=0.9)
    mock_extract.return_value = []

    response = client.post("/verify", json={"text": "Hmm, not sure what this is."})

    assert response.status_code == 200
    body = response.json()
    assert body["verdicts"] == []
    assert "no verifiable claims" in body["message"].lower()


@patch("app.routes.verify.insert_submission")
@patch("app.routes.verify.classify_intent")
@patch("app.routes.verify.extract_claims")
@patch("app.routes.verify.insert_claim")
@patch("app.routes.verify.process_claims")
@patch("app.routes.verify.insert_verdict")
def test_verdict_persisted_with_correct_db_claim_id_mapping(
    mock_insert_verdict,
    mock_process_claims,
    mock_insert_claim,
    mock_extract,
    mock_classify,
    mock_insert_submission,
):
    mock_insert_submission.return_value = {"id": "sub-1"}
    mock_classify.return_value = ContentIntent(label="FACTUAL_CLAIM", confidence=0.95)

    claim = Claim(
        claim_id="c1", text="Test claim", topic="test", specificity="specific", verifiability_score=0.9
    )
    mock_extract.return_value = [claim]
    # The claims table row gets its own DB-generated uuid, deliberately different
    # from the pipeline's "c1" - that's the mapping under test.
    mock_insert_claim.return_value = {"id": "db-uuid-xyz"}

    verdict = Verdict(
        claim_id="c1",
        label="TRUE",
        evidence_lines=[
            EvidenceLine(
                paraphrase="Rationale.", citation="https://example.com", stance="for", credibility_weight=0.9
            )
        ],
        summary_line="This confirms the claim.",
        confidence_score=0.9,
        citations=["https://example.com"],
        created_at=datetime.now(timezone.utc),
    )
    mock_process_claims.return_value = [verdict]
    mock_insert_verdict.return_value = {"id": "verdict-1"}

    response = client.post("/verify", json={"text": "Test claim"})

    assert response.status_code == 200
    mock_insert_verdict.assert_called_once()
    _, kwargs = mock_insert_verdict.call_args
    assert kwargs["claim_id"] == "db-uuid-xyz"  # not the pipeline id "c1"


# --- Sprint 18: combining ingested content for the Phase 1 pipeline ---


def test_combine_content_for_pipeline_includes_all_present_sections():
    content = StructuredContentObject(
        caption="A caption #test",
        transcript="Spoken words here.",
        visual_context="A scene description.",
        media_type="video",
    )
    result = _combine_content_for_pipeline(content)
    assert result == (
        "Caption: A caption #test\n\n"
        "Transcript: Spoken words here.\n\n"
        "Visual context: A scene description."
    )


def test_combine_content_for_pipeline_omits_missing_sections():
    content = StructuredContentObject(caption="Just a caption #test", media_type="text_post")
    assert _combine_content_for_pipeline(content) == "Caption: Just a caption #test"


def test_combine_content_for_pipeline_empty_when_nothing_present():
    content = StructuredContentObject(caption="", media_type="text_post")
    assert _combine_content_for_pipeline(content) == ""


# --- Sprint 13/18: upload endpoint (now runs the real ingestion + Phase 1 pipeline) ---


async def test_save_upload_to_temp_writes_a_real_file():
    fake_bytes = b"fake video bytes, not a real mp4"
    upload = UploadFile(filename="clip.mp4", file=io.BytesIO(fake_bytes))

    saved_path = await _save_upload_to_temp(upload)
    try:
        assert Path(saved_path).exists()
        assert Path(saved_path).read_bytes() == fake_bytes
        assert saved_path.endswith(".mp4")
    finally:
        shutil.rmtree(Path(saved_path).parent, ignore_errors=True)


@patch("app.routes.verify.cleanup_expired_media")
@patch("app.routes.verify.update_submission_media")
@patch("app.routes.verify.upload_media_file")
@patch("app.routes.verify.classify_intent")
@patch("app.routes.verify.route_and_assemble")
@patch("app.routes.verify.insert_submission")
def test_verify_upload_endpoint_runs_pipeline_on_ingested_content(
    mock_insert_submission,
    mock_route_and_assemble,
    mock_classify,
    mock_upload_media_file,
    mock_update_submission_media,
    mock_cleanup,
):
    mock_insert_submission.return_value = {"id": "sub-upload-1"}
    mock_route_and_assemble.return_value = StructuredContentObject(
        caption="Huge story! #factcheck",
        transcript="Something was said in the video.",
        visual_context=None,
        media_type="video",
    )
    mock_classify.return_value = ContentIntent(label="OPINION", confidence=0.9)

    response = client.post(
        "/verify/upload",
        files={"file": ("clip.mp4", b"fake video bytes", "video/mp4")},
        data={"caption": "Huge story! #factcheck"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["submission_id"] == "sub-upload-1"
    assert "opinion" in body["message"].lower()

    mock_cleanup.assert_called_once()
    mock_upload_media_file.assert_called_once()
    mock_update_submission_media.assert_called_once()

    # The ingested transcript/caption actually reached the Phase 1 pipeline -
    # the whole point of this sprint's wiring.
    text_seen_by_pipeline = mock_classify.call_args.args[0]
    assert "Something was said in the video." in text_seen_by_pipeline
    assert "Huge story! #factcheck" in text_seen_by_pipeline

    _, kwargs = mock_insert_submission.call_args
    # Must match the DB's real check constraint (see app/db/schema.sql):
    # input_type in ('text', 'upload', 'url').
    assert kwargs["input_type"] == "upload"


@patch("app.routes.verify.cleanup_expired_media")
@patch("app.routes.verify.update_submission_media")
@patch("app.routes.verify.upload_media_file")
@patch("app.routes.verify.route_and_assemble")
@patch("app.routes.verify.insert_submission")
def test_verify_upload_endpoint_no_extractable_content(
    mock_insert_submission,
    mock_route_and_assemble,
    mock_upload_media_file,
    mock_update_submission_media,
    mock_cleanup,
):
    mock_insert_submission.return_value = {"id": "sub-upload-2"}
    mock_route_and_assemble.return_value = StructuredContentObject(
        caption="", transcript=None, visual_context=None, media_type="video"
    )

    response = client.post("/verify/upload", files={"file": ("clip.mp4", b"bytes", "video/mp4")})

    assert response.status_code == 200
    body = response.json()
    assert body["claims"] == []
    assert body["message"] == NO_EXTRACTABLE_CONTENT_MESSAGE


# --- Sprint 14/18: media downloader + /verify/url (now runs the real pipeline) ---


def test_verify_url_empty_url_returns_400():
    response = client.post("/verify/url", json={"url": "   "})
    assert response.status_code == 400


@patch("app.routes.verify.cleanup_expired_media")
@patch("app.routes.verify.update_submission_media")
@patch("app.routes.verify.upload_media_file")
@patch("app.routes.verify.classify_intent")
@patch("app.routes.verify.route_and_assemble")
@patch("app.routes.verify.insert_submission")
@patch("app.routes.verify.download_media")
@patch("app.routes.verify.resolve_url")
def test_verify_url_successful_download_runs_pipeline(
    mock_resolve,
    mock_download,
    mock_insert_submission,
    mock_route_and_assemble,
    mock_classify,
    mock_upload_media_file,
    mock_update_submission_media,
    mock_cleanup,
):
    mock_resolve.return_value = ResolvedURL(
        canonical_url="https://www.instagram.com/reel/abc123/", platform="instagram"
    )
    mock_download.return_value = DownloadResult(
        success=True, file_path="/tmp/ingest_x/abc123.mp4", caption="A real caption"
    )
    mock_insert_submission.return_value = {"id": "sub-url-1"}
    mock_route_and_assemble.return_value = StructuredContentObject(
        caption="A real caption", transcript="Spoken content from the reel.", visual_context=None, media_type="video"
    )
    mock_classify.return_value = ContentIntent(label="UNRELATED", confidence=0.9)

    response = client.post(
        "/verify/url", json={"url": "https://www.instagram.com/reel/abc123/?igsh=xyz"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["claims"] == []
    assert "factual claim" in body["message"].lower()

    _, kwargs = mock_insert_submission.call_args
    # The resolved canonical URL is persisted, not the raw messy input.
    assert kwargs["raw_input"] == "https://www.instagram.com/reel/abc123/"
    assert kwargs["input_type"] == "url"

    mock_upload_media_file.assert_called_once()
    mock_route_and_assemble.assert_called_once_with(
        "/tmp/ingest_x/abc123.mp4", caption="A real caption", source_url="https://www.instagram.com/reel/abc123/"
    )
    text_seen_by_pipeline = mock_classify.call_args.args[0]
    assert "Spoken content from the reel." in text_seen_by_pipeline


@patch("app.routes.verify.cleanup_expired_media")
@patch("app.routes.verify.update_submission_media")
@patch("app.routes.verify.upload_media_file")
@patch("app.routes.verify.classify_intent")
@patch("app.routes.verify.route_and_assemble")
@patch("app.routes.verify.insert_submission")
@patch("app.routes.verify.download_media")
@patch("app.routes.verify.resolve_url")
def test_verify_url_caption_only_fallback_skips_storage_upload(
    mock_resolve,
    mock_download,
    mock_insert_submission,
    mock_route_and_assemble,
    mock_classify,
    mock_upload_media_file,
    mock_update_submission_media,
    mock_cleanup,
):
    mock_resolve.return_value = ResolvedURL(
        canonical_url="https://www.instagram.com/reel/rate-limited/", platform="instagram"
    )
    mock_download.return_value = DownloadResult(success=False, caption="Still got this caption")
    mock_insert_submission.return_value = {"id": "sub-url-2"}
    mock_route_and_assemble.return_value = StructuredContentObject(
        caption="Still got this caption", transcript=None, visual_context=None, media_type="text_post"
    )
    mock_classify.return_value = ContentIntent(label="UNRELATED", confidence=0.9)

    response = client.post("/verify/url", json={"url": "https://www.instagram.com/reel/rate-limited/"})

    assert response.status_code == 200
    # No file was downloaded, so nothing should be uploaded to Storage.
    mock_upload_media_file.assert_not_called()
    mock_route_and_assemble.assert_called_once_with(
        None, caption="Still got this caption", source_url="https://www.instagram.com/reel/rate-limited/"
    )


@patch("app.routes.verify.cleanup_expired_media")
@patch("app.routes.verify.insert_submission")
@patch("app.routes.verify.download_media")
@patch("app.routes.verify.resolve_url")
def test_verify_url_clean_failure_when_nothing_is_reachable(
    mock_resolve, mock_download, mock_insert_submission, mock_cleanup
):
    mock_resolve.return_value = ResolvedURL(
        canonical_url="https://www.instagram.com/reel/private-post/", platform="instagram"
    )
    mock_download.return_value = DownloadResult(
        success=False, caption=None, error="Could not download this content."
    )
    mock_insert_submission.return_value = {"id": "sub-url-3"}

    response = client.post("/verify/url", json={"url": "https://www.instagram.com/reel/private-post/"})

    assert response.status_code == 200
    body = response.json()
    assert body["claims"] == []
    assert body["message"] == URL_DOWNLOAD_FAILED_MESSAGE


@patch("app.routes.verify.cleanup_expired_media")
@patch("app.routes.verify.insert_submission")
@patch("app.routes.verify.download_media")
@patch("app.routes.verify.resolve_url")
def test_verify_url_photo_post_fallback_failure_gets_specific_message(
    mock_resolve, mock_download, mock_insert_submission, mock_cleanup
):
    # A photo post where the instaloader fallback also failed (see
    # media_downloader.PHOTO_POST_FALLBACK_FAILED_ERROR) - should get the
    # specific "we tried to fetch it directly but that also failed" message,
    # not the generic private/deleted/rate-limited one.
    mock_resolve.return_value = ResolvedURL(
        canonical_url="https://www.instagram.com/p/photo-post/", platform="instagram"
    )
    mock_download.return_value = DownloadResult(
        success=False, caption=None, error=PHOTO_POST_FALLBACK_FAILED_ERROR
    )
    mock_insert_submission.return_value = {"id": "sub-url-4"}

    response = client.post("/verify/url", json={"url": "https://www.instagram.com/p/photo-post/"})

    assert response.status_code == 200
    body = response.json()
    assert body["claims"] == []
    assert body["message"] == URL_PHOTO_POST_FALLBACK_FAILED_MESSAGE


@LIVE_SKIP
def test_verify_endpoint_live_end_to_end():
    start = time.perf_counter()
    response = client.post("/verify", json={"text": "The Eiffel Tower was completed in 1889."})
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert elapsed < 90, f"took {elapsed:.1f}s, expected under 90s"

    body = response.json()
    assert len(body["claims"]) >= 1
    assert len(body["verdicts"]) == len(body["claims"])
    verdict = body["verdicts"][0]
    assert verdict["label"] in {
        "TRUE", "FALSE", "PARTIALLY_TRUE", "MISLEADING", "UNVERIFIABLE", "OUTDATED", "SATIRE"
    }
    # Either real cited evidence, or (rarely) the no-usable-sources short circuit -
    # both are correct outcomes. produce_verdict never raises in the new design, so
    # there's no separate "graceful fallback" text to also check for here.
    assert verdict["citations"] or verdict["summary_line"] == NO_SOURCES_RATIONALE
    assert body["aggregate_label"] == verdict["label"]
