import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import FASTROUTER_API_KEY, SUPABASE_KEY, SUPABASE_URL, TAVILY_API_KEY
from app.main import app
from app.models.schemas import Claim, ContentIntent, RationaleSegment, Verdict
from app.pipeline import UNCITED_FALLBACK_RATIONALE

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
        rationale_segments=[
            RationaleSegment(text="Rationale.", segment_type="sourced_fact", citation="https://example.com")
        ],
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
    # Either a real cited verdict, or (rarely) the citation validator's graceful
    # fallback (Sprint 8/10) - both are correct outcomes, see pipeline.py.
    assert (
        verdict["citations"]
        or verdict["rationale_segments"][0]["text"] == UNCITED_FALLBACK_RATIONALE
    )
    assert body["aggregate_label"] == verdict["label"]
