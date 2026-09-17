import asyncio
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

from app.agents.research_agent import RawSearchResult
from app.db.client import list_source_credibility
from app.llm_client import MODEL_GPT4O_MINI, get_async_llm_client
from app.models.schemas import Claim, EvidenceItem, EvidencePackage

DEFAULT_CATEGORY = "Unclassified"
DEFAULT_WEIGHT = 0.4
EVIDENCE_CAP = 5

STANCE_SYSTEM_PROMPT = (
    "You judge whether each piece of evidence supports (\"for\") or contradicts "
    "(\"against\") a claim. If a piece of evidence is neutral, tangential, or merely "
    "provides context without disputing the claim, classify it as \"for\" (it does not "
    "contradict the claim). Only use \"against\" when the evidence actually disputes "
    "or contradicts the claim. Return one stance per numbered item, using the same index."
)


class _StanceItem(BaseModel):
    index: int
    stance: Literal["for", "against"]


class _StanceBatch(BaseModel):
    items: list[_StanceItem]


_credibility_cache: list[dict] | None = None


def _get_credibility_table() -> list[dict]:
    global _credibility_cache
    if _credibility_cache is None:
        _credibility_cache = list_source_credibility()
    return _credibility_cache


def _domain_from_url(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _lookup_credibility(domain: str, table: list[dict]) -> tuple[str, float]:
    for row in table:
        pattern = row["domain_pattern"].lower()
        if domain == pattern or domain.endswith("." + pattern):
            return row["category"], row["weight"]
    return DEFAULT_CATEGORY, DEFAULT_WEIGHT


async def _classify_stances(claim_text: str, results: list[RawSearchResult]) -> list[str]:
    if not results:
        return []
    numbered = "\n".join(f"{i}. {r.title} - {r.snippet}" for i, r in enumerate(results))
    user_content = f"Claim: {claim_text}\n\nEvidence items:\n{numbered}"

    completion = await get_async_llm_client().chat.completions.parse(
        model=MODEL_GPT4O_MINI,
        messages=[
            {"role": "system", "content": STANCE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format=_StanceBatch,
    )
    stance_map = {item.index: item.stance for item in completion.choices[0].message.parsed.items}
    return [stance_map.get(i, "for") for i in range(len(results))]


def compute_evidence_score(
    evidence_for: list[EvidenceItem], evidence_against: list[EvidenceItem]
) -> float:
    top_for = sorted(evidence_for, key=lambda e: e.credibility_weight, reverse=True)[:EVIDENCE_CAP]
    top_against = sorted(evidence_against, key=lambda e: e.credibility_weight, reverse=True)[:EVIDENCE_CAP]
    for_score = sum(e.credibility_weight for e in top_for)
    against_score = sum(e.credibility_weight for e in top_against)
    total = for_score + against_score
    if total == 0:
        return 0.5
    return for_score / total


async def rank_evidence(claim: Claim, raw_results: list[RawSearchResult]) -> EvidencePackage:
    # _get_credibility_table is sync (cached after its first Supabase call) - run it
    # off the event loop so it can never block other claims' concurrent work.
    credibility_table = await asyncio.to_thread(_get_credibility_table)
    stances = await _classify_stances(claim.text, raw_results)

    evidence_for: list[EvidenceItem] = []
    evidence_against: list[EvidenceItem] = []
    sources: list[str] = []

    for result, stance in zip(raw_results, stances):
        domain = _domain_from_url(result.url)
        category, weight = _lookup_credibility(domain, credibility_table)
        item = EvidenceItem(
            source_url=result.url,
            source_category=category,
            credibility_weight=weight,
            excerpt=result.snippet,
            stance=stance,
            published_date=result.published_date,
        )
        (evidence_for if stance == "for" else evidence_against).append(item)
        sources.append(result.url)

    evidence_for.sort(key=lambda i: i.credibility_weight, reverse=True)
    evidence_against.sort(key=lambda i: i.credibility_weight, reverse=True)

    confidence_raw = compute_evidence_score(evidence_for, evidence_against)

    return EvidencePackage(
        claim_id=claim.claim_id,
        evidence_for=evidence_for,
        evidence_against=evidence_against,
        sources=sources,
        confidence_raw=confidence_raw,
    )
