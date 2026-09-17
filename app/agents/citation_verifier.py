import asyncio
import logging
from typing import Optional

import httpx
import trafilatura
from pydantic import BaseModel

from app.citation_parsing import extract_citation_indices, split_sentences
from app.llm_client import MODEL_GPT4O_MINI, get_async_llm_client
from app.models.schemas import Verdict

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 10.0
CONFIDENCE_PENALTY_PER_REMOVED = 0.15
PAGE_TEXT_LIMIT = 6000

# A generic desktop-browser UA. Several major sources (Wikipedia, Reuters) block
# fetches regardless of UA - see the "can't fetch" handling below, which treats
# that as inconclusive rather than a verification failure precisely because of this.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CITATION_JUDGE_SYSTEM_PROMPT = (
    "You judge whether a web page's content supports a specific factual claim "
    "attributed to it. Read the claim and the page content, then decide: does the "
    "page actually support this claim? Answer strictly based on what the page says - "
    "if the page doesn't address the claim at all, or contradicts it, say no."
)


class _CitationJudgment(BaseModel):
    supported: bool
    reason: str


def _citation_attribution_map(rationale: str, num_citations: int) -> dict[int, list[str]]:
    """Maps each 1-based citation index to the sentence(s) that actually cite it.

    Verdict.citations includes every source offered to the Verdict Agent (Sprint 8),
    not just the ones it ended up citing - so an index with no entries here was never
    actually attributed anything and has nothing to verify against.
    """
    mapping: dict[int, list[str]] = {}
    for sentence in split_sentences(rationale):
        for idx in extract_citation_indices(sentence):
            if 1 <= idx <= num_citations:
                mapping.setdefault(idx, []).append(sentence)
    return mapping


async def _fetch_page_text(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.info("citation_verifier: could not fetch %s (%s)", url, exc)
        return None
    text = trafilatura.extract(response.text, include_comments=False)
    return text


async def _judge_citation(claim_text: str, page_text: str) -> _CitationJudgment:
    completion = await get_async_llm_client().chat.completions.parse(
        model=MODEL_GPT4O_MINI,
        messages=[
            {"role": "system", "content": CITATION_JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Attributed claim: {claim_text}\n\n"
                    f"Page content:\n{page_text[:PAGE_TEXT_LIMIT]}"
                ),
            },
        ],
        response_format=_CitationJudgment,
    )
    return completion.choices[0].message.parsed


async def _verify_one_citation(
    client: httpx.AsyncClient, index: int, url: str, attributed_text: str
) -> tuple[int, Optional[bool]]:
    """Returns (index, verdict) where verdict is True/False if checked, or None if the
    page couldn't be fetched at all - inconclusive, not a failure (see USER_AGENT note)."""
    page_text = await _fetch_page_text(client, url)
    if not page_text or not page_text.strip():
        return index, None
    judgment = await _judge_citation(attributed_text, page_text)
    if not judgment.supported:
        logger.info("citation_verifier: SOURCE_%d (%s) rejected: %s", index, url, judgment.reason)
    return index, judgment.supported


async def verify_citations(verdict: Verdict) -> Verdict:
    if not verdict.citations:
        return verdict

    attribution = _citation_attribution_map(verdict.rationale, len(verdict.citations))
    if not attribution:
        return verdict

    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        results = await asyncio.gather(
            *(
                _verify_one_citation(client, idx, verdict.citations[idx - 1], " ".join(sentences))
                for idx, sentences in attribution.items()
            )
        )

    failed_indices = {idx for idx, supported in results if supported is False}
    if not failed_indices:
        return verdict

    kept_citations = [url for i, url in enumerate(verdict.citations, start=1) if i not in failed_indices]
    new_confidence = verdict.confidence_score * (
        (1 - CONFIDENCE_PENALTY_PER_REMOVED) ** len(failed_indices)
    )

    return verdict.model_copy(update={"citations": kept_citations, "confidence_score": new_confidence})
