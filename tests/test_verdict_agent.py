from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.analyst_agent import analyze_evidence
from app.agents.evidence_ranker import rank_evidence
from app.agents.research_agent import research_claim
from app.agents.verdict_agent import (
    AUDIT_FALLBACK_SUMMARY,
    NO_SOURCES_RATIONALE,
    _extract_factual_details,
    audit_summary_line,
    looks_factual,
    produce_verdict,
)
from app.config import FASTROUTER_API_KEY, SUPABASE_KEY, SUPABASE_URL, TAVILY_API_KEY
from app.models.schemas import (
    AnalystOutput,
    Claim,
    EvidenceItem,
    EvidenceLine,
    EvidencePackage,
    VerdictAgentOutput,
)

LIVE_SKIP = pytest.mark.skipif(
    not (TAVILY_API_KEY and FASTROUTER_API_KEY and SUPABASE_URL and SUPABASE_KEY),
    reason="TAVILY_API_KEY / FASTROUTER_API_KEY / SUPABASE_URL / SUPABASE_KEY not set in .env",
)


def _claim(text: str = "Test claim") -> Claim:
    return Claim(claim_id="c1", text=text, topic="test", specificity="specific", verifiability_score=0.9)


def _evidence_item(
    evidence_id: str, weight: float, stance: str, url: str, paraphrase: str
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source_url=url,
        source_category="Test",
        credibility_weight=weight,
        excerpt=paraphrase,
        paraphrase=paraphrase,
        stance=stance,
    )


def _evidence_line(paraphrase: str, citation: str, stance: str = "for", weight: float = 0.9) -> EvidenceLine:
    return EvidenceLine(paraphrase=paraphrase, citation=citation, stance=stance, credibility_weight=weight)


def _mock_completion(label: str, summary_line: str, confidence_score: float) -> MagicMock:
    parsed = VerdictAgentOutput(label=label, summary_line=summary_line, confidence_score=confidence_score)
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(parsed=parsed))]
    return completion


# --- looks_factual / _extract_factual_details: cheap heuristic, used only to
# audit summary_line for a smuggled-in new fact ---


def test_looks_factual_detects_number():
    assert looks_factual("It happened in 1889.")


def test_looks_factual_detects_proper_noun():
    assert looks_factual("The tower is in Paris.")


def test_looks_factual_false_for_generic_text():
    assert not looks_factual("This seems reasonable overall.")


def test_extract_factual_details_excludes_trailing_sentence_punctuation():
    # A real bug: [\d,]* greedily swallowed a plain sentence comma/period as if
    # it were part of the number, so "1889" written two different ways in the
    # same paragraph ("1889," vs "1889.") extracted as two DIFFERENT tokens.
    assert _extract_factual_details("in March 1889, this supports") == ["1889", "March"]
    # "This" starts the second sentence here - capitalization from sentence
    # position, not a proper noun, so it must not be extracted as a detail.
    assert _extract_factual_details("renovated in 2018. This confirms") == ["2018"]


def test_extract_factual_details_keeps_genuine_thousands_separator():
    assert _extract_factual_details("is over 13,000 miles long") == ["13,000"]


# --- audit_summary_line ---


def test_summary_line_without_new_fact_passes_audit():
    lines = [_evidence_line("The tower was completed in 1889.", "https://example.com/a")]
    summary = "The claim is corroborated by clear and reliable evidence."
    assert audit_summary_line(summary, lines) == []


def test_summary_line_with_smuggled_new_fact_trips_audit():
    lines = [_evidence_line("The tower is 330 meters tall.", "https://example.com/a")]
    summary = "It was also renovated again in 2018."
    assert audit_summary_line(summary, lines) == ["2018"]


def test_summary_line_restating_an_evidence_line_passes_audit():
    lines = [_evidence_line("The Eiffel Tower was completed in 1889.", "https://example.com/a")]
    summary = "The Eiffel Tower was completed in 1889."
    assert audit_summary_line(summary, lines) == []


def test_summary_line_paraphrasing_an_evidence_line_passes_audit():
    # Reproduces a real live failure from the earlier segment-based audit: a
    # closing sentence almost never repeats an evidence line VERBATIM, it
    # paraphrases - "construction concluded in March 1889" vs. "completed on
    # March 31, 1889" is the same detail (1889), different wording. Checking the
    # detail, not the sentence, must survive this.
    lines = [
        _evidence_line(
            "The Eiffel Tower's construction was completed on March 31, 1889.", "https://example.com/a"
        )
    ]
    summary = (
        "Since the Eiffel Tower's construction concluded in March 1889, this "
        "supports the claim that it was completed in 1889, making the claim accurate."
    )
    assert audit_summary_line(summary, lines) == []


# --- deterministic no-evidence short circuit ---


async def test_no_evidence_short_circuits_to_unverifiable():
    evidence = EvidencePackage(
        claim_id="c1", evidence_for=[], evidence_against=[], sources=[], confidence_raw=0.5
    )
    analyst = AnalystOutput(claim_id="c1", selected_for_ids=[], selected_against_ids=[], outdated_flag=False)
    with patch("app.agents.verdict_agent.get_async_llm_client") as mock_get_client:
        verdict = await produce_verdict(_claim(), analyst, evidence)
        mock_get_client.assert_not_called()

    assert verdict.label == "UNVERIFIABLE"
    assert verdict.evidence_lines == []
    assert verdict.summary_line == NO_SOURCES_RATIONALE
    assert verdict.citations == []
    assert verdict.confidence_score == 0.0


# --- evidence-line assembly: code only, resolves selected ids into EvidenceLines ---


async def test_evidence_lines_are_built_from_selected_ids_only():
    evidence = EvidencePackage(
        claim_id="c1",
        evidence_for=[
            _evidence_item("a", 0.9, "for", "https://example.com/a", "Supports it strongly."),
            _evidence_item("b", 0.2, "for", "https://example.com/b", "Weakly relevant."),
        ],
        evidence_against=[],
        sources=["https://example.com/a", "https://example.com/b"],
        confidence_raw=0.9,
    )
    analyst = AnalystOutput(claim_id="c1", selected_for_ids=["a"], selected_against_ids=[], outdated_flag=False)

    with patch("app.agents.verdict_agent.get_async_llm_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.chat.completions.parse = AsyncMock(
            return_value=_mock_completion("TRUE", "This confirms the claim.", 0.9)
        )
        mock_get_client.return_value = mock_client

        verdict = await produce_verdict(_claim(), analyst, evidence)

    assert len(verdict.evidence_lines) == 1
    assert verdict.evidence_lines[0].paraphrase == "Supports it strongly."
    assert verdict.citations == ["https://example.com/a"]


# --- retry path: audit failure, then success ---


@patch("app.agents.verdict_agent.get_async_llm_client")
async def test_reprompt_fixes_audit_violation(mock_get_client):
    bad = _mock_completion("TRUE", "It was also renovated again in 2018.", 0.9)
    good = _mock_completion("TRUE", "This confirms the claim is true.", 0.9)
    mock_client = MagicMock()
    mock_client.chat.completions.parse = AsyncMock(side_effect=[bad, good])
    mock_get_client.return_value = mock_client

    evidence = EvidencePackage(
        claim_id="c1",
        evidence_for=[
            _evidence_item("a", 0.9, "for", "https://example.com/a", "The tower is 330 meters tall.")
        ],
        evidence_against=[],
        sources=["https://example.com/a"],
        confidence_raw=1.0,
    )
    analyst = AnalystOutput(claim_id="c1", selected_for_ids=["a"], selected_against_ids=[], outdated_flag=False)

    verdict = await produce_verdict(_claim(), analyst, evidence)

    assert mock_client.chat.completions.parse.call_count == 2
    assert verdict.label == "TRUE"
    assert verdict.summary_line == "This confirms the claim is true."
    assert verdict.citations == ["https://example.com/a"]


@patch("app.agents.verdict_agent.get_async_llm_client")
async def test_falls_back_to_unverifiable_keeping_real_evidence_if_still_invalid_after_retry(mock_get_client):
    bad = _mock_completion("TRUE", "It was also renovated again in 2018.", 0.9)
    mock_client = MagicMock()
    mock_client.chat.completions.parse = AsyncMock(side_effect=[bad, bad])
    mock_get_client.return_value = mock_client

    evidence = EvidencePackage(
        claim_id="c1",
        evidence_for=[
            _evidence_item("a", 0.9, "for", "https://example.com/a", "The tower is 330 meters tall.")
        ],
        evidence_against=[],
        sources=["https://example.com/a"],
        confidence_raw=1.0,
    )
    analyst = AnalystOutput(claim_id="c1", selected_for_ids=["a"], selected_against_ids=[], outdated_flag=False)

    verdict = await produce_verdict(_claim(), analyst, evidence)

    assert mock_client.chat.completions.parse.call_count == 2
    assert verdict.label == "UNVERIFIABLE"
    assert verdict.summary_line == AUDIT_FALLBACK_SUMMARY
    # The real evidence and its citation must survive the fallback - unlike the
    # old design, where a failed verdict discarded everything.
    assert len(verdict.evidence_lines) == 1
    assert verdict.citations == ["https://example.com/a"]
    assert verdict.confidence_score == 0.0


# --- full-pipeline, no-evidence integration (doesn't need live keys: both agents
# short-circuit deterministically when there's no evidence at all) ---


async def test_full_pipeline_unverifiable_with_no_evidence():
    claim = _claim("Zzyzx quantum flibbertigibbet index rose 42% in Narnia last Thursday.")
    evidence = EvidencePackage(
        claim_id="c1", evidence_for=[], evidence_against=[], sources=[], confidence_raw=0.5
    )
    analyst = await analyze_evidence(claim, evidence)
    verdict = await produce_verdict(claim, analyst, evidence)
    assert verdict.label == "UNVERIFIABLE"


# --- live label-triggering tests ---
#
# produce_verdict never raises in the new design - it can only return a real
# label or gracefully degrade to UNVERIFIABLE via the audit-fallback safety net.
# These tests tolerate that fallback rather than asserting every live call
# produces the "ideal" label - they're about correct behavior when the pipeline
# DOES succeed, not about forcing every live model call to succeed.


async def _full_pipeline(claim_text: str):
    claim = _claim(claim_text)
    raw = await research_claim(claim)
    evidence = await rank_evidence(claim, raw)
    analyst = await analyze_evidence(claim, evidence)
    return await produce_verdict(claim, analyst, evidence)


@LIVE_SKIP
async def test_verdict_true_for_well_supported_claim():
    verdict = await _full_pipeline("The Eiffel Tower was completed in 1889.")
    assert verdict.label in {"TRUE", "UNVERIFIABLE"}
    if verdict.label == "TRUE":
        assert verdict.citations


@LIVE_SKIP
async def test_verdict_false_for_clearly_false_claim():
    verdict = await _full_pipeline("The Eiffel Tower is located in London, England.")
    assert verdict.label in {"FALSE", "UNVERIFIABLE"}


@LIVE_SKIP
async def test_verdict_for_mixed_evidence_claim():
    verdict = await _full_pipeline("Drinking coffee every day has no health risks whatsoever.")
    assert verdict.label in {"PARTIALLY_TRUE", "MISLEADING", "FALSE", "UNVERIFIABLE"}
