import asyncio
import logging
from typing import Optional

import httpx
import trafilatura
from pydantic import BaseModel

from app.llm_client import MODEL_GPT4O_MINI, get_async_llm_client
from app.models.schemas import EvidenceLine, Verdict

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


async def _fetch_page_text(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.info("citation_verifier: could not fetch %s (%s)", url, exc)
        return None
    return trafilatura.extract(response.text, include_comments=False)


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


async def _verify_one_line(
    client: httpx.AsyncClient, line: EvidenceLine
) -> tuple[EvidenceLine, Optional[bool]]:
    """Returns (line, verdict) where verdict is True/False if checked, or None if
    the page couldn't be fetched at all - inconclusive, not a failure (see
    USER_AGENT note). summary_line is never passed in here - it's exempt from
    citation verification by design (see verdict_agent: it's audited separately,
    at write time, for smuggled-in facts, not fetched against a source)."""
    page_text = await _fetch_page_text(client, line.citation)
    if not page_text or not page_text.strip():
        return line, None
    judgment = await _judge_citation(line.paraphrase, page_text)
    if not judgment.supported:
        logger.info(
            "citation_verifier: %s rejected for %r: %s", line.citation, line.paraphrase, judgment.reason
        )
    return line, judgment.supported


async def verify_citations(verdict: Verdict) -> Verdict:
    if not verdict.evidence_lines:
        return verdict

    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        results = await asyncio.gather(
            *(_verify_one_line(client, line) for line in verdict.evidence_lines)
        )

    # asyncio.gather preserves input order, so results lines up 1:1 with
    # verdict.evidence_lines - no identity/id() bookkeeping needed.
    kept_lines = [line for line, supported in results if supported is not False]
    failed_count = len(verdict.evidence_lines) - len(kept_lines)
    if failed_count == 0:
        return verdict

    kept_citations = list(dict.fromkeys(line.citation for line in kept_lines))
    new_confidence = verdict.confidence_score * ((1 - CONFIDENCE_PENALTY_PER_REMOVED) ** failed_count)

    return verdict.model_copy(
        update={
            "evidence_lines": kept_lines,
            "citations": kept_citations,
            "confidence_score": new_confidence,
        }
    )
