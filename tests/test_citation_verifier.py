from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.agents.citation_verifier import _CitationJudgment, verify_citations
from app.config import FASTROUTER_API_KEY
from app.models.schemas import EvidenceLine, Verdict

LIVE_SKIP = pytest.mark.skipif(not FASTROUTER_API_KEY, reason="FASTROUTER_API_KEY not set in .env")


def _verdict(
    lines: list[EvidenceLine],
    citations: list[str],
    summary_line: str = "Summary.",
    confidence: float = 0.9,
) -> Verdict:
    return Verdict(
        claim_id="c1",
        label="TRUE",
        evidence_lines=lines,
        summary_line=summary_line,
        confidence_score=confidence,
        citations=citations,
        created_at=datetime.now(timezone.utc),
    )


def _evidence_line(paraphrase: str, citation: str) -> EvidenceLine:
    return EvidenceLine(paraphrase=paraphrase, citation=citation, stance="for", credibility_weight=0.8)


async def test_no_evidence_lines_short_circuits():
    verdict = _verdict([], citations=[])
    with patch("app.agents.citation_verifier._fetch_page_text") as mock_fetch:
        result = await verify_citations(verdict)
        mock_fetch.assert_not_called()
    assert result == verdict


@patch("app.agents.citation_verifier._judge_citation")
@patch("app.agents.citation_verifier._fetch_page_text")
async def test_mismatched_evidence_line_is_fully_removed_not_just_citation(mock_fetch, mock_judge):
    mock_fetch.return_value = "page text"
    mock_judge.side_effect = [
        _CitationJudgment(supported=True, reason="Matches the fact."),
        _CitationJudgment(supported=False, reason="Page never mentions this at all."),
    ]

    verdict = _verdict(
        [
            _evidence_line("Real fact.", "https://example.com/real"),
            _evidence_line("Fabricated fact.", "https://example.com/fake"),
        ],
        citations=["https://example.com/real", "https://example.com/fake"],
        confidence=0.9,
    )

    result = await verify_citations(verdict)

    assert len(result.evidence_lines) == 1
    assert result.evidence_lines[0].citation == "https://example.com/real"
    assert result.citations == ["https://example.com/real"]
    assert result.confidence_score == pytest.approx(0.9 * 0.85)


@patch("app.agents.citation_verifier._judge_citation")
@patch("app.agents.citation_verifier._fetch_page_text")
async def test_shared_citation_url_survives_if_one_of_its_lines_is_valid(mock_fetch, mock_judge):
    # The old (segment-based) design purged a URL from citations entirely if ANY
    # segment citing it failed - too coarse: two lines can legitimately share one
    # citation, and one being fabricated shouldn't erase the other's real one.
    # Per-line removal must not over-punish a URL that still has a valid line.
    mock_fetch.return_value = "page text"
    mock_judge.side_effect = [
        _CitationJudgment(supported=True, reason="Matches."),
        _CitationJudgment(supported=False, reason="Never mentions this."),
    ]

    shared_url = "https://example.com/shared"
    verdict = _verdict(
        [
            _evidence_line("Real fact from this page.", shared_url),
            _evidence_line("Fabricated fact also attributed to this page.", shared_url),
        ],
        citations=[shared_url],
        confidence=0.9,
    )

    result = await verify_citations(verdict)

    assert len(result.evidence_lines) == 1
    assert result.evidence_lines[0].paraphrase == "Real fact from this page."
    assert result.citations == [shared_url]
    assert result.confidence_score == pytest.approx(0.9 * 0.85)


async def test_unfetchable_evidence_line_is_kept_not_penalized():
    # A site blocking automated fetches (Wikipedia, Reuters - confirmed during
    # development) must not be treated the same as a genuinely mismatched line -
    # "couldn't check" is not "checked and failed."
    verdict = _verdict(
        [_evidence_line("Fact one.", "https://example.com/blocked")],
        citations=["https://example.com/blocked"],
        confidence=0.9,
    )
    with patch("app.agents.citation_verifier._fetch_page_text") as mock_fetch, patch(
        "app.agents.citation_verifier._judge_citation"
    ) as mock_judge:
        mock_fetch.return_value = None  # simulates a blocked/failed fetch
        result = await verify_citations(verdict)
        mock_judge.assert_not_called()

    assert result.citations == ["https://example.com/blocked"]
    assert len(result.evidence_lines) == 1
    assert result.confidence_score == 0.9


async def test_summary_line_is_never_fetched_or_verified_regardless_of_content():
    verdict = _verdict(
        [_evidence_line("Real fact.", "https://example.com/real")],
        citations=["https://example.com/real"],
        summary_line=(
            "This closing sentence fabricates a date of 1929 and cites "
            "https://example.com/never-fetched."
        ),
    )
    with patch("app.agents.citation_verifier._fetch_page_text") as mock_fetch, patch(
        "app.agents.citation_verifier._judge_citation"
    ) as mock_judge:
        mock_fetch.return_value = "some page text"
        mock_judge.return_value = _CitationJudgment(supported=True, reason="ok")
        result = await verify_citations(verdict)

    assert mock_fetch.call_count == 1
    fetched_urls = [call.args[1] for call in mock_fetch.call_args_list]
    assert "https://example.com/never-fetched" not in fetched_urls
    assert result.summary_line == verdict.summary_line


@LIVE_SKIP
async def test_verify_citations_live_catches_mismatched_citation():
    # MDN's HTTP status reference pages are confirmed reachable without bot-blocking
    # (unlike wikipedia.org/reuters.com, which reject automated fetches outright) and
    # have stable, unambiguous reference content - unlike a live news homepage, which
    # changes constantly and made an earlier version of this test flaky.
    mdn_404_page = "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404"
    verdict = _verdict(
        [
            _evidence_line(
                "A 404 Not Found status means the server could not find the requested resource.",
                mdn_404_page,
            ),
            _evidence_line("The Great Wall of China is over 13,000 miles long.", mdn_404_page),
        ],
        citations=[mdn_404_page],
        confidence=0.9,
    )

    result = await verify_citations(verdict)

    # The accurate line survives, so its shared citation URL survives too - only
    # the fabricated line is removed (see test_shared_citation_url_survives_if_one_of_its_lines_is_valid
    # for the deterministic version of this same invariant).
    assert len(result.evidence_lines) == 1
    assert result.evidence_lines[0].paraphrase.startswith("A 404 Not Found")
    assert result.citations == [mdn_404_page]
    assert result.confidence_score == pytest.approx(0.9 * 0.85)
