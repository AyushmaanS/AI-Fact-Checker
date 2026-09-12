from openai import OpenAI

from app.config import OPENAI_API_KEY
from app.models.schemas import ContentIntent

_client: OpenAI | None = None

SYSTEM_PROMPT = (
    "You classify a piece of text by its intent, ahead of any fact-checking. "
    "Categories:\n"
    "- FACTUAL_CLAIM: makes a checkable assertion about reality (a stat, an event, a quote, a policy outcome).\n"
    "- OPINION: a subjective judgment or preference, not a checkable fact.\n"
    "- SATIRE_COMEDY: intentionally humorous or absurd, not meant to be taken literally.\n"
    "- FICTIONAL_CREATIVE: fiction, poetry, or other creative writing with no claim to real-world truth.\n"
    "- UNRELATED: greetings, small talk, or content with no factual or opinion content to classify.\n"
    "Give a confidence score between 0 and 1 for your label."
)


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY must be set in .env")
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def classify_intent(text: str) -> ContentIntent:
    completion = _get_client().chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format=ContentIntent,
    )
    return completion.choices[0].message.parsed
