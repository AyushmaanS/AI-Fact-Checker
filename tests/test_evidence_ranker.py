from unittest.mock import patch

import pytest

from app.agents.evidence_ranker import _domain_from_url, _lookup_credibility, rank_evidence
from app.agents.research_agent import RawSearchResult, research_claim
from app.config import FASTROUTER_API_KEY, SUPABASE_KEY, SUPABASE_URL, TAVILY_API_KEY
from app.models.schemas import Claim

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
def test_rank_evidence_sorts_by_credibility_descending(mock_table, mock_stances):
    mock_table.return_value = FAKE_CREDIBILITY_TABLE
    mock_stances.return_value = ["for", "for", "against"]

    package = rank_evidence(_claim(), _raw_results())

    assert len(package.evidence_for) == 2
    assert len(package.evidence_against) == 1
    # Reuters (0.82) must rank above Twitter (0.10) within evidence_for.
    assert package.evidence_for[0].source_url.startswith("https://www.reuters.com")
    assert package.evidence_for[1].source_url.startswith("https://twitter.com")
    assert package.evidence_against[0].source_category == "Major National Newspapers"
    assert 0.0 <= package.confidence_raw <= 1.0


@patch("app.agents.evidence_ranker._classify_stances")
@patch("app.agents.evidence_ranker._get_credibility_table")
def test_rank_evidence_handles_no_results(mock_table, mock_stances):
    mock_table.return_value = FAKE_CREDIBILITY_TABLE
    mock_stances.return_value = []

    package = rank_evidence(_claim(), [])

    assert package.evidence_for == []
    assert package.evidence_against == []
    assert package.confidence_raw == 0.5


@pytest.mark.skipif(
    not (TAVILY_API_KEY and FASTROUTER_API_KEY and SUPABASE_URL and SUPABASE_KEY),
    reason="TAVILY_API_KEY / FASTROUTER_API_KEY / SUPABASE_URL / SUPABASE_KEY not set in .env",
)
def test_rank_evidence_live():
    claim = Claim(
        claim_id="c1",
        text="The Eiffel Tower was completed in 1889.",
        topic="history",
        specificity="specific",
        verifiability_score=1.0,
    )
    raw_results = research_claim(claim)
    package = rank_evidence(claim, raw_results)

    assert len(package.evidence_for) + len(package.evidence_against) == len(raw_results)
    for lst in (package.evidence_for, package.evidence_against):
        weights = [item.credibility_weight for item in lst]
        assert weights == sorted(weights, reverse=True)
