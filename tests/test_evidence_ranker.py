from unittest.mock import patch

import pytest

from app.agents.evidence_ranker import (
    _domain_from_url,
    _lookup_credibility,
    _StanceItem,
    compute_evidence_score,
    rank_evidence,
)
from app.agents.research_agent import RawSearchResult, research_claim
from app.config import FASTROUTER_API_KEY, SUPABASE_KEY, SUPABASE_URL, TAVILY_API_KEY
from app.models.schemas import Claim, EvidenceItem

FAKE_CREDIBILITY_TABLE = [
    {"domain_pattern": "reuters.com", "category": "Major Wire Services", "weight": 0.82},
    {"domain_pattern": "nytimes.com", "category": "Major National Newspapers", "weight": 0.75},
    {"domain_pattern": "twitter.com", "category": "Social Media", "weight": 0.10},
]


def _claim() -> Claim:
    return Claim(
        claim_id="c1",
        text="Test claim",
        topic="test",
        specificity="specific",
        verifiability_score=0.9,
    )


def _raw_results() -> list[RawSearchResult]:
    return [
        RawSearchResult(
            title="Twitter post", url="https://twitter.com/x/status/1",
            snippet="supports it", query="q1",
        ),
        RawSearchResult(
            title="Reuters article", url="https://www.reuters.com/article",
            snippet="confirms it", query="q1",
        ),
        RawSearchResult(
            title="NYT article", url="https://www.nytimes.com/article",
            snippet="disputes it", query="q1",
        ),
    ]


def _stance_items(*stances: str) -> list[_StanceItem]:
    return [
        _StanceItem(index=i, stance=stance, paraphrase=f"Paraphrase {i}.")
        for i, stance in enumerate(stances)
    ]


def _evidence_item(weight: float, stance: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id="test_0",
        source_url="https://example.com",
        source_category="Test",
        credibility_weight=weight,
        excerpt="evidence",
        paraphrase="A paraphrase.",
        stance=stance,
    )


def test_compute_evidence_score_normal_mixed_matches_manual_calculation():
    evidence_for = [_evidence_item(0.9, "for"), _evidence_item(0.7, "for"), _evidence_item(0.5, "for")]
    evidence_against = [_evidence_item(0.8, "against"), _evidence_item(0.3, "against")]

    score = compute_evidence_score(evidence_for, evidence_against)

    expected = (0.9 + 0.7 + 0.5) / (0.9 + 0.7 + 0.5 + 0.8 + 0.3)
    assert score == pytest.approx(expected)
    assert 0.0 < score < 1.0


def test_compute_evidence_score_viral_misinformation_against_dominates():
    # 2 highly-credible sources against, 15 low-credibility sources for - the cap
    # means volume can't drown out quality: against must still win.
    evidence_against = [_evidence_item(0.95, "against"), _evidence_item(0.90, "against")]
    evidence_for = [_evidence_item(0.20, "for") for _ in range(15)]

    score = compute_evidence_score(evidence_for, evidence_against)

    assert score < 0.5


def test_compute_evidence_score_thin_coverage_is_not_the_default():
    evidence_for = [_evidence_item(0.65, "for"), _evidence_item(0.70, "for")]
    evidence_against = [_evidence_item(0.60, "against")]

    score = compute_evidence_score(evidence_for, evidence_against)

    assert score != 0.5


def test_domain_from_url_strips_www():
    assert _domain_from_url("https://www.reuters.com/article/123") == "reuters.com"
    assert _domain_from_url("https://reuters.com/article/123") == "reuters.com"


def test_lookup_credibility_fallback_for_unknown_domain():
    category, weight = _lookup_credibility("some-random-blog.example", FAKE_CREDIBILITY_TABLE)
    assert category == "Unclassified"
    assert weight == 0.4


def test_lookup_credibility_matches_known_domain():
    category, weight = _lookup_credibility("reuters.com", FAKE_CREDIBILITY_TABLE)
    assert category == "Major Wire Services"
    assert weight == 0.82


@patch("app.agents.evidence_ranker._classify_stances")
@patch("app.agents.evidence_ranker._get_credibility_table")
async def test_rank_evidence_sorts_by_credibility_descending(mock_table, mock_stances):
    mock_table.return_value = FAKE_CREDIBILITY_TABLE
    mock_stances.return_value = _stance_items("for", "for", "against")

    package = await rank_evidence(_claim(), _raw_results())

    assert len(package.evidence_for) == 2
    assert len(package.evidence_against) == 1
    # Reuters (0.82) must rank above Twitter (0.10) within evidence_for.
    assert package.evidence_for[0].source_url.startswith("https://www.reuters.com")
    assert package.evidence_for[1].source_url.startswith("https://twitter.com")
    assert package.evidence_against[0].source_category == "Major National Newspapers"
    assert 0.0 <= package.confidence_raw <= 1.0


@patch("app.agents.evidence_ranker._classify_stances")
@patch("app.agents.evidence_ranker._get_credibility_table")
async def test_rank_evidence_handles_no_results(mock_table, mock_stances):
    mock_table.return_value = FAKE_CREDIBILITY_TABLE
    mock_stances.return_value = []

    package = await rank_evidence(_claim(), [])

    assert package.evidence_for == []
    assert package.evidence_against == []
    assert package.confidence_raw == 0.5


@patch("app.agents.evidence_ranker._classify_stances")
@patch("app.agents.evidence_ranker._get_credibility_table")
async def test_every_evidence_item_has_real_paraphrase_and_unique_id(mock_table, mock_stances):
    mock_table.return_value = FAKE_CREDIBILITY_TABLE
    mock_stances.return_value = _stance_items("for", "for", "against")

    package = await rank_evidence(_claim(), _raw_results())

    all_items = package.evidence_for + package.evidence_against
    assert len(all_items) == 3
    assert all(item.paraphrase for item in all_items)
    assert len({item.evidence_id for item in all_items}) == 3


@pytest.mark.skipif(
    not (TAVILY_API_KEY and FASTROUTER_API_KEY and SUPABASE_URL and SUPABASE_KEY),
    reason="TAVILY_API_KEY / FASTROUTER_API_KEY / SUPABASE_URL / SUPABASE_KEY not set in .env",
)
async def test_rank_evidence_live():
    claim = Claim(
        claim_id="c1",
        text="The Eiffel Tower was completed in 1889.",
        topic="history",
        specificity="specific",
        verifiability_score=1.0,
    )
    raw_results = await research_claim(claim)
    package = await rank_evidence(claim, raw_results)

    assert len(package.evidence_for) + len(package.evidence_against) == len(raw_results)
    all_items = package.evidence_for + package.evidence_against
    assert all(item.paraphrase for item in all_items)
    assert len({item.evidence_id for item in all_items}) == len(all_items)
    for lst in (package.evidence_for, package.evidence_against):
        weights = [item.credibility_weight for item in lst]
        assert weights == sorted(weights, reverse=True)
