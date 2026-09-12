from app.llm_client import MODEL_GPT4O_MINI, get_llm_client
from app.models.schemas import ContentIntent

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


def classify_intent(text: str) -> ContentIntent:
    completion = get_llm_client().chat.completions.parse(
        model=MODEL_GPT4O_MINI,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format=ContentIntent,
    )
    return completion.choices[0].message.parsed
