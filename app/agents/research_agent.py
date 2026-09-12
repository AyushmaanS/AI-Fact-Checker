from typing import Optional

from pydantic import BaseModel
from tavily import TavilyClient

from app.config import TAVILY_API_KEY
from app.llm_client import MODEL_GPT4O_MINI, get_llm_client
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


_tavily_client: TavilyClient | None = None


def _get_tavily_client() -> TavilyClient:
    global _tavily_client
    if _tavily_client is None:
        if not TAVILY_API_KEY:
            raise RuntimeError("TAVILY_API_KEY must be set in .env")
        _tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
    return _tavily_client


def decompose_query(claim_text: str) -> list[str]:
    completion = get_llm_client().chat.completions.parse(
        model=MODEL_GPT4O_MINI,
        messages=[
            {"role": "system", "content": DECOMPOSER_SYSTEM_PROMPT},
            {"role": "user", "content": claim_text},
        ],
        response_format=_QueryDecomposition,
    )
    return completion.choices[0].message.parsed.queries


def research_claim(claim: Claim) -> list[RawSearchResult]:
    queries = decompose_query(claim.text)
    tavily = _get_tavily_client()

    results: list[RawSearchResult] = []
    for query in queries:
        response = tavily.search(query=query, max_results=5)
        for item in response.get("results", []):
            results.append(
                RawSearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                    published_date=item.get("published_date"),
                    query=query,
                )
            )
    return results
