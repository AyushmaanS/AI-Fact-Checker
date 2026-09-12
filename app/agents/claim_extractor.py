from typing import Literal

from pydantic import BaseModel

from app.llm_client import MODEL_GPT4O, get_llm_client
from app.models.schemas import Claim

SYSTEM_PROMPT = (
    "You extract atomic, independently-verifiable factual claims from a piece of text. "
    "Each claim should be a single checkable assertion - split compound sentences into "
    "separate claims rather than combining multiple facts into one. For each claim, tag:\n"
    "- topic: a short topic label (e.g. 'politics', 'health', 'history', 'science').\n"
    "- specificity: 'specific' if it names a precise figure, date, or named entity; "
    "'general' if it's a broader/vaguer assertion.\n"
    "- verifiability_score: 0.0-1.0, how checkable this claim is against public sources.\n"
    "If the text contains no verifiable factual claims (e.g. it's pure opinion, a greeting, "
    "or too vague to check), return an empty list of claims. Do not invent claims that "
    "aren't actually asserted in the text."
)


class _ExtractedClaim(BaseModel):
    text: str
    topic: str
    specificity: Literal["specific", "general"]
    verifiability_score: float


class _ClaimExtraction(BaseModel):
    # OpenAI structured outputs require an object at the schema root, not a bare
    # array, hence this wrapper instead of response_format=list[Claim] directly.
    claims: list[_ExtractedClaim]


def extract_claims(text: str) -> list[Claim]:
    completion = get_llm_client().chat.completions.parse(
        model=MODEL_GPT4O,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format=_ClaimExtraction,
    )
    extracted = completion.choices[0].message.parsed.claims
    return [
        Claim(
            claim_id=f"c{i + 1}",
            text=item.text,
            topic=item.topic,
            specificity=item.specificity,
            verifiability_score=item.verifiability_score,
        )
        for i, item in enumerate(extracted)
    ]
