from datetime import datetime, timezone

from app.models.schemas import (
    Claim,
    EvidenceItem,
    EvidenceLine,
    EvidencePackage,
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
            evidence_lines=[
                EvidenceLine(
                    paraphrase="Confirmed by the source.",
                    citation="https://example.com",
                    stance="for",
                    credibility_weight=0.8,
                )
            ],
            summary_line="This confirms the claim.",
            confidence_score=0.9,
            citations=["https://example.com"],
            created_at=datetime.now(timezone.utc),
        )
        assert verdict.label == label


def test_verdict_accepts_empty_evidence_lines():
    # The no-sources short circuit produces a Verdict with no evidence at all.
    # Nothing in the schema forbids this: unlike the old design, the LLM never
    # writes a citation directly onto a Verdict-level field, so there's no
    # cross-field invariant left for a validator to enforce.
    verdict = Verdict(
        claim_id="c1",
        label="UNVERIFIABLE",
        evidence_lines=[],
        summary_line="No usable sources were found.",
        confidence_score=0.0,
        citations=[],
        created_at=datetime.now(timezone.utc),
    )
    assert verdict.evidence_lines == []


def test_evidence_item_has_id_and_paraphrase():
    item = EvidenceItem(
        evidence_id="c1_0",
        source_url="https://example.com",
        source_category="Major Wire Services",
        credibility_weight=0.82,
        excerpt="An excerpt.",
        paraphrase="A one-line paraphrase of the excerpt.",
        stance="for",
    )
    assert item.evidence_id == "c1_0"
    assert item.paraphrase


def test_evidence_package_shape():
    item = EvidenceItem(
        evidence_id="c1_0",
        source_url="https://example.com",
        source_category="Major Wire Services",
        credibility_weight=0.82,
        excerpt="An excerpt.",
        paraphrase="A one-line paraphrase.",
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
