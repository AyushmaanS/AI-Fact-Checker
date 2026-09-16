import time

import pytest

from app.agents.evidence_ranker import rank_evidence
from app.agents.research_agent import research_claim
from app.config import FASTROUTER_API_KEY, SUPABASE_KEY, SUPABASE_URL, TAVILY_API_KEY
from app.models.schemas import Claim
from app.pipeline import research_claims

pytestmark = pytest.mark.skipif(
    not (TAVILY_API_KEY and FASTROUTER_API_KEY and SUPABASE_URL and SUPABASE_KEY),
    reason="TAVILY_API_KEY / FASTROUTER_API_KEY / SUPABASE_URL / SUPABASE_KEY not set in .env",
)


def _sample_claims() -> list[Claim]:
    texts = [
        "The Eiffel Tower was completed in 1889.",
        "Mount Everest is the tallest mountain on Earth.",
        "The Great Wall of China is over 13,000 miles long.",
    ]
    return [
        Claim(claim_id=f"c{i}", text=t, topic="test", specificity="specific", verifiability_score=0.9)
        for i, t in enumerate(texts)
    ]


async def test_concurrent_pipeline_is_faster_than_sequential():
    claims = _sample_claims()

    start = time.perf_counter()
    sequential_results = []
    for claim in claims:
        raw = await research_claim(claim)
        sequential_results.append(await rank_evidence(claim, raw))
    sequential_time = time.perf_counter() - start

    start = time.perf_counter()
    concurrent_results = await research_claims(claims)
    concurrent_time = time.perf_counter() - start

    print(
        f"\nSequential: {sequential_time:.1f}s  |  "
        f"Concurrent: {concurrent_time:.1f}s  |  "
        f"Speedup: {sequential_time / concurrent_time:.2f}x"
    )

    assert len(sequential_results) == len(concurrent_results) == 3
    assert concurrent_time < sequential_time
