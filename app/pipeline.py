import asyncio

from app.agents.evidence_ranker import rank_evidence
from app.agents.research_agent import research_claim
from app.models.schemas import Claim, EvidencePackage

MAX_CONCURRENT_RESEARCH = 8


async def _research_and_rank(claim: Claim, semaphore: asyncio.Semaphore) -> EvidencePackage:
    async with semaphore:
        raw_results = await research_claim(claim)
        return await rank_evidence(claim, raw_results)


async def research_claims(claims: list[Claim]) -> list[EvidencePackage]:
    """Research + rank every claim, capped at MAX_CONCURRENT_RESEARCH concurrent claims."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_RESEARCH)
    results: list[EvidencePackage] = []

    # Batching in groups of MAX_CONCURRENT_RESEARCH plus a same-sized semaphore is
    # belt-and-suspenders here (a batch never exceeds the semaphore's own cap) - the
    # semaphore is what actually matters if this is ever called with a larger batch
    # size or restructured to submit everything at once.
    for i in range(0, len(claims), MAX_CONCURRENT_RESEARCH):
        batch = claims[i : i + MAX_CONCURRENT_RESEARCH]
        batch_results = await asyncio.gather(
            *(_research_and_rank(claim, semaphore) for claim in batch)
        )
        results.extend(batch_results)

    return results
