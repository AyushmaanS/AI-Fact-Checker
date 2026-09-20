from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.analyst_agent import _AnalystLLMOutput, _select_top_ids, analyze_evidence
from app.agents.evidence_ranker import rank_evidence
from app.agents.research_agent import research_claim
from app.config import FASTROUTER_API_KEY, SUPABASE_KEY, SUPABASE_URL, TAVILY_API_KEY
from app.models.schemas import Claim, EvidenceItem, EvidencePackage


def _claim() -> Claim:
    return Claim(
        claim_id="c1", text="Test claim", topic="test", specificity="specific", verifiability_score=0.9
    )


def _evidence_item(evidence_id: str, weight: float, stance: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source_url=f"https://example.com/{evidence_id}",
        source_category="Test",
        credibility_weight=weight,
        excerpt="evidence",
        paraphrase="A paraphrase.",
        stance=stance,
    )


def _mock_completion(outdated_flag: bool) -> MagicMock:
    parsed = _AnalystLLMOutput(outdated_flag=outdated_flag)
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(parsed=parsed))]
    return completion


# --- selection: plain code, no LLM call ---


def test_select_top_ids_picks_three_highest_by_credibility_without_llm():
    items = [
        _evidence_item("a", 0.3, "for"),
        _evidence_item("b", 0.9, "for"),
        _evidence_item("c", 0.5, "for"),
        _evidence_item("d", 0.7, "for"),
    ]
    assert _select_top_ids(items) == ["b", "d", "c"]


def test_select_top_ids_handles_zero_items():
    assert _select_top_ids([]) == []


async def test_fully_empty_evidence_never_calls_llm():
    evidence = EvidencePackage(
        claim_id="c1", evidence_for=[], evidence_against=[], sources=[], confidence_raw=0.5
    )
    with patch("app.agents.analyst_agent.get_async_llm_client") as mock_get_client:
        result = await analyze_evidence(_claim(), evidence)
        mock_get_client.assert_not_called()

    assert result.selected_for_ids == []
    assert result.selected_against_ids == []
    assert result.outdated_flag is False


@patch("app.agents.analyst_agent.get_async_llm_client")
async def test_selected_ids_survive_alongside_llm_outdated_flag(mock_get_client):
    mock_client = MagicMock()
    mock_client.chat.completions.parse = AsyncMock(return_value=_mock_completion(outdated_flag=True))
    mock_get_client.return_value = mock_client

    evidence = EvidencePackage(
        claim_id="c1",
        evidence_for=[_evidence_item("a", 0.9, "for")],
        evidence_against=[_evidence_item("b", 0.8, "against")],
        sources=["https://example.com/a", "https://example.com/b"],
        confidence_raw=0.6,
    )

    result = await analyze_evidence(_claim(), evidence)

    assert result.selected_for_ids == ["a"]
    assert result.selected_against_ids == ["b"]
    assert result.outdated_flag is True


@pytest.mark.skipif(
    not (TAVILY_API_KEY and FASTROUTER_API_KEY and SUPABASE_URL and SUPABASE_KEY),
    reason="TAVILY_API_KEY / FASTROUTER_API_KEY / SUPABASE_URL / SUPABASE_KEY not set in .env",
)
async def test_analyze_evidence_live():
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

    assert isinstance(result.outdated_flag, bool)
    assert set(result.selected_for_ids) <= {item.evidence_id for item in evidence.evidence_for}
    assert set(result.selected_against_ids) <= {item.evidence_id for item in evidence.evidence_against}
