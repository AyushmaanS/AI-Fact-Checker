import pytest

from app.agents.claim_extractor import extract_claims
from app.config import FASTROUTER_API_KEY

pytestmark = pytest.mark.skipif(
    not FASTROUTER_API_KEY,
    reason="FASTROUTER_API_KEY not set in .env",
)


def test_single_claim_sentence():
    claims = extract_claims("The Great Wall of China is over 13,000 miles long.")
    assert len(claims) == 1
    assert claims[0].text


def test_multi_claim_paragraph():
    text = (
        "The Eiffel Tower was completed in 1889. It was designed by Gustave "
        "Eiffel's engineering company. The tower is located in Paris, France."
    )
    claims = extract_claims(text)
    assert 2 <= len(claims) <= 4, f"expected 2-4 claims, got {len(claims)}: {claims}"
    ids = [c.claim_id for c in claims]
    assert len(ids) == len(set(ids))


def test_zero_claim_text():
    claims = extract_claims("Wow, what a beautiful sunset!")
    assert claims == []
