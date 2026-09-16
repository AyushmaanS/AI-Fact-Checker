from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.analyst_agent import (
    NO_AGAINST_EVIDENCE_TEXT,
    NO_FOR_EVIDENCE_TEXT,
    _AnalystLLMOutput,
    _detect_fringe_vs_consensus,
    analyze_evidence,
)
from app.agents.evidence_ranker import rank_evidence
from app.agents.research_agent import research_claim
from app.config import FASTROUTER_API_KEY, SUPABASE_KEY, SUPABASE_URL, TAVILY_API_KEY
from app.models.schemas import Claim, EvidenceItem, EvidencePackage


def _claim() -> Claim:
    return Claim(
        claim_id="c1", text="Test claim", topic="test", specificity="specific", verifiability_score=0.9
    )


def _evidence_item(weight: float, stance: str, excerpt: str = "evidence") -> EvidenceItem:
    return EvidenceItem(
        source_url="https://example.com",
        source_category="Test",
        credibility_weight=weight,
        excerpt=excerpt,
        stance=stance,
    )


def _mock_completion(for_summary: str, against_summary: str, outdated_flag: bool) -> MagicMock:
    parsed = _AnalystLLMOutput(
        for_summary=for_summary, against_summary=against_summary, outdated_flag=outdated_flag
    )
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(parsed=parsed))]
    return completion


async def test_fully_empty_evidence_never_calls_llm():
    evidence = EvidencePackage(
        claim_id="c1", evidence_for=[], evidence_against=[], sources=[], confidence_raw=0.5
    )
    with patch("app.agents.analyst_agent.get_async_llm_client") as mock_get_client:
        result = await analyze_evidence(_claim(), evidence)
        mock_get_client.assert_not_called()

    assert result.for_summary == NO_FOR_EVIDENCE_TEXT
    assert result.against_summary == NO_AGAINST_EVIDENCE_TEXT
    assert result.outdated_flag is False
    assert result.fringe_vs_consensus_note is None


@patch("app.agents.analyst_agent.get_async_llm_client")
async def test_one_sided_evidence_gets_deterministic_against_summary(mock_get_client):
    # Even if the (mocked) model returns junk for against_summary, it must be ignored -
    # this is the "deliberately one-sided EvidencePackage" case the sprint asks for.
    mock_client = MagicMock()
    mock_client.chat.completions.parse = AsyncMock(
        return_value=_mock_completion(
            for_summary="Multiple credible sources confirm this.",
            against_summary="this should never surface",
            outdated_flag=False,
        )
    )
    mock_get_client.return_value = mock_client

    evidence = EvidencePackage(
        claim_id="c1",
        evidence_for=[_evidence_item(0.9, "for")],
        evidence_against=[],
        sources=["https://example.com"],
        confidence_raw=1.0,
    )

    result = await analyze_evidence(_claim(), evidence)

    assert result.against_summary == NO_AGAINST_EVIDENCE_TEXT
    assert result.for_summary == "Multiple credible sources confirm this."


def test_fringe_vs_consensus_detected():
    # 10 credible items (9 for @0.95, 1 against @0.8) = exactly 90% credible consensus,
    # plus one low-credibility dissenter to trigger the note.
    evidence_for = [_evidence_item(0.95, "for") for _ in range(9)]
    evidence_against = [_evidence_item(0.8, "against"), _evidence_item(0.2, "against")]
    note = _detect_fringe_vs_consensus(evidence_for, evidence_against)
    assert note is not None
    assert "90%" in note


def test_fringe_vs_consensus_not_triggered_when_credible_sources_split():
    evidence_for = [_evidence_item(0.8, "for") for _ in range(5)]
    evidence_against = [_evidence_item(0.8, "against") for _ in range(5)]
    assert _detect_fringe_vs_consensus(evidence_for, evidence_against) is None


def test_fringe_vs_consensus_not_triggered_when_dissent_is_also_credible():
    evidence_for = [_evidence_item(0.95, "for") for _ in range(9)]
    evidence_against = [_evidence_item(0.8, "against")]  # 0.8 >= 0.75, not "low-credibility"
    assert _detect_fringe_vs_consensus(evidence_for, evidence_against) is None


@pytest.mark.skipif(
    not (TAVILY_API_KEY and FASTROUTER_API_KEY and SUPABASE_URL and SUPABASE_KEY),
    reason="TAVILY_API_KEY / FASTROUTER_API_KEY / SUPABASE_URL / SUPABASE_KEY not set in .env",
)
async def test_analyze_evidence_live_one_sided():
    claim = Claim(
        claim_id="c1",
        text="The Eiffel Tower was completed in 1889.",
        topic="history",
        specificity="specific",
        verifiability_score=1.0,
    )
    raw = await research_claim(claim)
    evidence = await rank_evidence(claim, raw)
    result = await analyze_evidence(claim, evidence)

    assert result.for_summary
    assert result.against_summary
    if not evidence.evidence_against:
        assert result.against_summary == NO_AGAINST_EVIDENCE_TEXT
