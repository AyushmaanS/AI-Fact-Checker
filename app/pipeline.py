import asyncio
import logging
from datetime import datetime, timezone

from app.agents.analyst_agent import analyze_evidence
from app.agents.citation_verifier import verify_citations
from app.agents.evidence_ranker import rank_evidence
from app.agents.research_agent import research_claim
from app.agents.verdict_agent import VerdictCitationError, produce_verdict
from app.models.schemas import Claim, EvidencePackage, RationaleSegment, Verdict

logger = logging.getLogger(__name__)

MAX_CONCURRENT_RESEARCH = 8

UNCITED_FALLBACK_RATIONALE = (
    "This claim's evidence was researched, but a fully-cited verdict could not be "
    "produced after a correction attempt, so no verdict is being reported rather "
    "than risk shipping an inadequately-sourced one."
)


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


async def _process_one_claim(claim: Claim, semaphore: asyncio.Semaphore) -> Verdict:
    async with semaphore:
        raw_results = await research_claim(claim)
        evidence = await rank_evidence(claim, raw_results)
        analyst = await analyze_evidence(claim, evidence)

        try:
            verdict = await produce_verdict(claim, analyst, evidence)
        except VerdictCitationError as exc:
            # The citation validator's retry-then-fail safety net (Sprint 8) is
            # working as designed here, not malfunctioning - refusing to ship an
            # under-cited verdict. The API still owes the caller a Verdict object
            # per claim, so this degrades to an explained UNVERIFIABLE rather than
            # letting the exception propagate into a 500 for the whole request.
            logger.warning("claim %s: %s", claim.claim_id, exc)
            return Verdict(
                claim_id=claim.claim_id,
                label="UNVERIFIABLE",
                rationale_segments=[
                    RationaleSegment(
                        text=UNCITED_FALLBACK_RATIONALE, segment_type="connective_reasoning"
                    )
                ],
                confidence_score=0.0,
                citations=[],
                created_at=datetime.now(timezone.utc),
            )

        return await verify_citations(verdict)


async def process_claims(claims: list[Claim]) -> list[Verdict]:
    """Full per-claim pipeline (research -> rank -> analyze -> verdict -> citation
    verify), capped at MAX_CONCURRENT_RESEARCH concurrent claims end-to-end."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_RESEARCH)
    results: list[Verdict] = []

    for i in range(0, len(claims), MAX_CONCURRENT_RESEARCH):
        batch = claims[i : i + MAX_CONCURRENT_RESEARCH]
        batch_results = await asyncio.gather(
            *(_process_one_claim(claim, semaphore) for claim in batch)
        )
        results.extend(batch_results)

    return results
