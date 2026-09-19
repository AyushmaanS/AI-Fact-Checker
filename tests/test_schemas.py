from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    Claim,
    EvidenceItem,
    EvidencePackage,
    RationaleSegment,
    Verdict,
)


def test_claim_round_trip():
    claim = Claim(
        claim_id="c1",
        text="The Eiffel Tower is in Paris.",
        topic="geography",
        specificity="specific",
        verifiability_score=0.99,
    )
    assert claim.model_dump()["claim_id"] == "c1"


def test_verdict_accepts_all_seven_labels():
    for label in [
        "TRUE",
        "FALSE",
        "PARTIALLY_TRUE",
        "MISLEADING",
        "UNVERIFIABLE",
        "OUTDATED",
        "SATIRE",
    ]:
        verdict = Verdict(
            claim_id="c1",
            label=label,
            rationale_segments=[
                RationaleSegment(
                    text="Confirmed by the source.",
                    segment_type="sourced_fact",
                    citation="https://example.com",
                )
            ],
            confidence_score=0.9,
            citations=["https://example.com"],
            created_at=datetime.now(timezone.utc),
        )
        assert verdict.label == label


def test_verdict_rejects_sourced_fact_without_citation():
    with pytest.raises(ValidationError):
        Verdict(
            claim_id="c1",
            label="TRUE",
            rationale_segments=[
                RationaleSegment(text="A fact with no source.", segment_type="sourced_fact", citation=None)
            ],
            confidence_score=0.9,
            citations=["https://example.com"],
            created_at=datetime.now(timezone.utc),
        )


def test_verdict_accepts_connective_segment_without_citation():
    verdict = Verdict(
        claim_id="c1",
        label="TRUE",
        rationale_segments=[
            RationaleSegment(text="This confirms the claim.", segment_type="connective_reasoning")
        ],
        confidence_score=0.9,
        citations=[],
        created_at=datetime.now(timezone.utc),
    )
    assert verdict.rationale_segments[0].citation is None


def test_evidence_package_shape():
    item = EvidenceItem(
        source_url="https://example.com",
        source_category="Major Wire Services",
        credibility_weight=0.82,
        excerpt="An excerpt.",
        stance="for",
    )
    package = EvidencePackage(
        claim_id="c1",
        evidence_for=[item],
        evidence_against=[],
        sources=["https://example.com"],
        confidence_raw=0.8,
    )
    assert package.evidence_for[0].stance == "for"
    assert package.evidence_against == []
