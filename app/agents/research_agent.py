import asyncio
from typing import Optional

from pydantic import BaseModel
from tavily import AsyncTavilyClient

from app.config import TAVILY_API_KEY
from app.llm_client import MODEL_GPT4O_MINI, get_async_llm_client
from app.models.schemas import Claim

DECOMPOSER_SYSTEM_PROMPT = (
    "Given a factual claim, generate 3 to 5 distinct, targeted web search queries "
    "that would help verify or refute it. Cover different angles: the core fact "
    "itself, official/primary sources, recent news coverage, and potential "
    "counter-evidence. Return only the query strings."
)


class _QueryDecomposition(BaseModel):
    queries: list[str]


class RawSearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    published_date: Optional[str] = None
    query: str  # which decomposed query surfaced this result


_tavily_client: AsyncTavilyClient | None = None


def _get_async_tavily_client() -> AsyncTavilyClient:
    global _tavily_client
    if _tavily_client is None:
        if not TAVILY_API_KEY:
            raise RuntimeError("TAVILY_API_KEY must be set in .env")
        _tavily_client = AsyncTavilyClient(api_key=TAVILY_API_KEY)
    return _tavily_client


async def decompose_query(claim_text: str) -> list[str]:
    completion = await get_async_llm_client().chat.completions.parse(
        model=MODEL_GPT4O_MINI,
        messages=[
            {"role": "system", "content": DECOMPOSER_SYSTEM_PROMPT},
            {"role": "user", "content": claim_text},
        ],
        response_format=_QueryDecomposition,
    )
    return completion.choices[0].message.parsed.queries


async def _search_one_query(tavily: AsyncTavilyClient, query: str) -> list[RawSearchResult]:
    response = await tavily.search(query=query, max_results=5)
    return [
        RawSearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("content", ""),
            published_date=item.get("published_date"),
            query=query,
        )
        for item in response.get("results", [])
    ]


async def research_claim(claim: Claim) -> list[RawSearchResult]:
    queries = await decompose_query(claim.text)
    tavily = _get_async_tavily_client()

    # The claim's own 3-5 queries also run concurrently, not just claim-vs-claim.
    batches = await asyncio.gather(*(_search_one_query(tavily, q) for q in queries))
    return [item for batch in batches for item in batch]
