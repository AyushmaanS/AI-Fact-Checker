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
