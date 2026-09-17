from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.citation_verifier import _CitationJudgment, verify_citations
from app.config import FASTROUTER_API_KEY
from app.models.schemas import Verdict

LIVE_SKIP = pytest.mark.skipif(not FASTROUTER_API_KEY, reason="FASTROUTER_API_KEY not set in .env")


def _verdict(rationale: str, citations: list[str], confidence: float = 0.9) -> Verdict:
    return Verdict(
        claim_id="c1",
        label="TRUE",
        rationale=rationale,
        confidence_score=confidence,
        citations=citations,
        created_at=datetime.now(timezone.utc),
    )


async def test_no_citations_short_circuits():
    verdict = _verdict("No sources were found.", citations=[])
    with patch("app.agents.citation_verifier._fetch_page_text") as mock_fetch:
        result = await verify_citations(verdict)
        mock_fetch.assert_not_called()
    assert result == verdict


async def test_unreferenced_citation_is_never_fetched_or_touched():
    # citations[] can include sources the Verdict Agent was offered but never
    # actually cited in the text - nothing to verify those against.
    verdict = _verdict(
        "Fact one [SOURCE_1].",
        citations=["https://example.com/a", "https://example.com/b"],
    )
    with patch("app.agents.citation_verifier._fetch_page_text") as mock_fetch, patch(
        "app.agents.citation_verifier._judge_citation"
    ) as mock_judge:
        mock_fetch.return_value = "some page text"
        mock_judge.return_value = _CitationJudgment(supported=True, reason="ok")
        result = await verify_citations(verdict)

    mock_fetch.assert_called_once()  # only SOURCE_1, not the unreferenced SOURCE_2
    assert result.citations == ["https://example.com/a", "https://example.com/b"]


@patch("app.agents.citation_verifier._judge_citation")
@patch("app.agents.citation_verifier._fetch_page_text")
async def test_mismatched_citation_is_removed_and_confidence_reduced(mock_fetch, mock_judge):
    mock_fetch.return_value = "page text"
    mock_judge.side_effect = [
        _CitationJudgment(supported=True, reason="Matches the fact."),
        _CitationJudgment(supported=False, reason="Page never mentions this at all."),
    ]

    verdict = _verdict(
        "Real fact [SOURCE_1]. Fabricated fact [SOURCE_2].",
        citations=["https://example.com/real", "https://example.com/fake"],
        confidence=0.9,
    )

    result = await verify_citations(verdict)

    assert result.citations == ["https://example.com/real"]
    assert result.confidence_score == pytest.approx(0.9 * 0.85)


async def test_unfetchable_citation_is_kept_not_penalized():
    # A site blocking automated fetches (Wikipedia, Reuters - confirmed during
    # development) must not be treated the same as a genuinely mismatched citation -
    # "couldn't check" is not "checked and failed."
    verdict = _verdict("Fact one [SOURCE_1].", citations=["https://example.com/blocked"], confidence=0.9)
    with patch("app.agents.citation_verifier._fetch_page_text") as mock_fetch, patch(
        "app.agents.citation_verifier._judge_citation"
    ) as mock_judge:
        mock_fetch.return_value = None  # simulates a blocked/failed fetch
        result = await verify_citations(verdict)
        mock_judge.assert_not_called()

    assert result.citations == ["https://example.com/blocked"]
    assert result.confidence_score == 0.9


@LIVE_SKIP
async def test_verify_citations_live_catches_mismatched_citation():
    # MDN's HTTP status reference pages are confirmed reachable without bot-blocking
    # (unlike wikipedia.org/reuters.com, which reject automated fetches outright) and
    # have stable, unambiguous reference content - unlike a live news homepage, which
    # changes constantly and made an earlier version of this test flaky.
    mdn_404_page = "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404"
    verdict = _verdict(
        rationale=(
            "A 404 Not Found status means the server could not find the requested "
            "resource [SOURCE_1]. The Great Wall of China is over 13,000 miles long [SOURCE_2]."
        ),
        citations=[mdn_404_page, mdn_404_page],
        confidence=0.9,
    )

    result = await verify_citations(verdict)

    assert result.citations == [mdn_404_page]
    assert result.confidence_score == pytest.approx(0.9 * 0.85)
