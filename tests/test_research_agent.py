from unittest.mock import MagicMock, patch

import pytest

from app.agents.research_agent import research_claim
from app.config import FASTROUTER_API_KEY, TAVILY_API_KEY
from app.models.schemas import Claim


def _sample_claim() -> Claim:
    return Claim(
        claim_id="c1",
        text="The Eiffel Tower was completed in 1889.",
        topic="history",
        specificity="specific",
        verifiability_score=1.0,
    )


@patch("app.agents.research_agent.decompose_query")
@patch("app.agents.research_agent._get_tavily_client")
def test_research_claim_with_mocked_tavily(mock_get_client, mock_decompose):
    mock_decompose.return_value = ["Eiffel Tower completion year"]
    mock_client = MagicMock()
    mock_client.search.return_value = {
        "results": [
            {
                "title": "Eiffel Tower - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Eiffel_Tower",
                "content": "The Eiffel Tower was completed in 1889...",
            }
        ]
    }
    mock_get_client.return_value = mock_client

    results = research_claim(_sample_claim())

    assert len(results) == 1
    assert results[0].url == "https://en.wikipedia.org/wiki/Eiffel_Tower"
    assert results[0].snippet.startswith("The Eiffel Tower")
    assert results[0].query == "Eiffel Tower completion year"
    mock_client.search.assert_called_once()


@pytest.mark.skipif(
    not (TAVILY_API_KEY and FASTROUTER_API_KEY),
    reason="TAVILY_API_KEY / FASTROUTER_API_KEY not set in .env",
)
def test_research_claim_live():
    results = research_claim(_sample_claim())
    assert len(results) > 0
    for r in results:
        assert r.url.startswith("http")
        assert r.title
