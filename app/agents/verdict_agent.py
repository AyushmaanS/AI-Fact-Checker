import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from app.agents.evidence_ranker import EVIDENCE_CAP
from app.citation_parsing import extract_citation_indices, split_sentences
from app.llm_client import MODEL_GPT4O, get_async_llm_client
from app.models.schemas import AnalystOutput, Claim, EvidenceItem, EvidencePackage, Verdict

NO_SOURCES_RATIONALE = "No usable sources were found to verify or refute this claim."

_ASSERTION_VERBS = {
    "is", "are", "was", "were", "has", "have", "had", "confirms", "confirmed",
    "shows", "showed", "states", "stated", "reports", "reported", "found",
    "indicates", "indicated", "according", "occurred", "began", "completed",
    "announced", "declared", "revealed",
}

# Live testing (Sprint 8) found the model reliably writes a bare, uncited
# verdict-announcement sentence - "The claim that X is [false/misleading/...]
# based on the evidence" - no matter how explicitly the prompt says every
# sentence needs a citation. Two rounds of stronger prompt wording didn't fix
# it, so this exempts that specific pattern instead of chasing it with prompts:
# it's restating the verdict itself, not introducing a fact of its own.
_VERDICT_ANNOUNCEMENT_PATTERN = re.compile(
    r"\bthe claim\b.{0,150}\b("
    r"true|false|partially true|misleading|unverifiable|outdated|satire|"
    r"accurate|inaccurate|correct|incorrect|supported|unsupported|disputed|"
    r"unfounded|baseless|untrue|debunked|refuted|disproven|erroneous|"
    r"unsubstantiated|substantiated|verified|confirmed|contradicted|"
    r"corroborated|corroborates|supports?|substantiates?"
    r")\b",
    re.IGNORECASE,
)


def _is_verdict_announcement(sentence: str) -> bool:
    return bool(_VERDICT_ANNOUNCEMENT_PATTERN.search(sentence))


VERDICT_SYSTEM_PROMPT = (
    "You are a fact-checking verdict writer. Given a claim, an adversarial analyst's "
    "summary of supporting and contradicting evidence, and a numbered list of sources, "
    "write a verdict.\n\n"
    "CITATION RULE (mandatory): every factual statement in your rationale must be "
    "immediately followed by a citation reference like [SOURCE_1]; you may not make "
    "any factual assertion without one. This includes your concluding/verdict-announcement "
    "sentence(s) - if a sentence restates what the evidence shows or announces the label "
    "(e.g. \"the claim is false\"), it needs a citation too, even if that just means "
    "reusing a [SOURCE_N] you already cited earlier for that fact. A sentence that is "
    "PURE framing with no factual content at all (e.g. \"In summary:\") does not need one. "
    "Cite sources individually, like [SOURCE_1] [SOURCE_2] - not combined in one bracket "
    "like [SOURCE_1, SOURCE_2]. Only cite sources from the numbered list you were given - "
    "never invent a source number.\n\n"
    "Choose exactly one label:\n"
    "- TRUE: evidence clearly supports the claim, no credible contradiction.\n"
    "- FALSE: evidence clearly contradicts the claim.\n"
    "- PARTIALLY_TRUE: the claim is accurate in part but omits or misstates something material.\n"
    "- MISLEADING: technically defensible but creates a false impression (e.g. true fact, wrong context).\n"
    "- UNVERIFIABLE: the evidence is too thin or inconclusive to decide either way.\n"
    "- OUTDATED: was accurate at some point but the evidence shows the situation has since changed.\n"
    "- SATIRE: the claim is satire or comedy, not a genuine factual assertion.\n\n"
    "Set confidence_score (0.0-1.0) reflecting how confident you are in the label given "
    "the evidence balance you were shown."
)


class _VerdictLLMOutput(BaseModel):
    label: Literal[
        "TRUE", "FALSE", "PARTIALLY_TRUE", "MISLEADING", "UNVERIFIABLE", "OUTDATED", "SATIRE"
    ]
    rationale: str
    confidence_score: float


class VerdictCitationError(RuntimeError):
    """Raised when the model still produces uncited factual sentences after one retry."""


def _numbered_sources(evidence: EvidencePackage) -> list[EvidenceItem]:
    # Evidence lists are already sorted by credibility_weight descending (Evidence
    # Ranker) - capping here keeps the prompt to the highest-quality candidates.
    combined = evidence.evidence_for[:EVIDENCE_CAP] + evidence.evidence_against[:EVIDENCE_CAP]
    seen: set[str] = set()
    deduped: list[EvidenceItem] = []
    for item in combined:
        if item.source_url not in seen:
            seen.add(item.source_url)
            deduped.append(item)
    return deduped


def _format_source_list(sources: list[EvidenceItem]) -> str:
    return "\n".join(
        f"[SOURCE_{i}] ({item.stance}, weight {item.credibility_weight:.2f}) "
        f"{item.excerpt} - {item.source_url}"
        for i, item in enumerate(sources, start=1)
    )


def _sentence_looks_factual(sentence: str) -> bool:
    if re.search(r"\d", sentence):
        return True
    words = sentence.strip().split()
    if any(w[0].isupper() for w in words[1:] if w and w[0].isalpha()):
        return True
    lowered = sentence.lower()
    return any(re.search(rf"\b{re.escape(verb)}\b", lowered) for verb in _ASSERTION_VERBS)


def _find_uncited_factual_sentences(rationale: str, num_sources: int) -> list[str]:
    problems = []
    for sentence in split_sentences(rationale):
        if _is_verdict_announcement(sentence):
            continue
        if not _sentence_looks_factual(sentence):
            continue
        indices = extract_citation_indices(sentence)
        if not any(1 <= i <= num_sources for i in indices):
            problems.append(sentence)
    return problems


def _build_user_content(
    claim: Claim, analyst: AnalystOutput, evidence: EvidencePackage, sources: list[EvidenceItem]
) -> str:
    return (
        f"Claim: {claim.text}\n\n"
        f"Supporting evidence summary: {analyst.for_summary}\n\n"
        f"Contradicting evidence summary: {analyst.against_summary}\n\n"
        f"Evidence balance score (credibility-weighted, 0=all against, 1=all for): "
        f"{evidence.confidence_raw:.2f}\n"
        f"Outdated flag (analyst's judgment): {analyst.outdated_flag}\n\n"
        f"Numbered sources:\n{_format_source_list(sources)}"
    )


async def produce_verdict(claim: Claim, analyst: AnalystOutput, evidence: EvidencePackage) -> Verdict:
    sources = _numbered_sources(evidence)

    if not sources:
        return Verdict(
            claim_id=claim.claim_id,
            label="UNVERIFIABLE",
            rationale=NO_SOURCES_RATIONALE,
            confidence_score=0.0,
            citations=[],
            created_at=datetime.now(timezone.utc),
        )

    client = get_async_llm_client()
    messages = [
        {"role": "system", "content": VERDICT_SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_content(claim, analyst, evidence, sources)},
    ]

    completion = await client.chat.completions.parse(
        model=MODEL_GPT4O, messages=messages, response_format=_VerdictLLMOutput
    )
    parsed = completion.choices[0].message.parsed
    missing = _find_uncited_factual_sentences(parsed.rationale, len(sources))

    if missing:
        correction = (
            "Your rationale had factual statement(s) with no valid [SOURCE_N] citation:\n"
            + "\n".join(f'- "{s}"' for s in missing)
            + "\n\nThis includes concluding or verdict-announcing sentences - if one of the "
            "sentences above restates what the evidence shows rather than introducing a new "
            "fact, fix it by appending the same [SOURCE_N] you already used earlier for that "
            "fact, not by inventing a new source. Rewrite the ENTIRE rationale so every "
            f"factual sentence cites a source between [SOURCE_1] and [SOURCE_{len(sources)}], "
            "cited individually (e.g. [SOURCE_1] [SOURCE_2], never combined in one bracket). "
            "Do not invent new sources."
        )
        messages.append({"role": "assistant", "content": parsed.rationale})
        messages.append({"role": "user", "content": correction})

        completion = await client.chat.completions.parse(
            model=MODEL_GPT4O, messages=messages, response_format=_VerdictLLMOutput
        )
        parsed = completion.choices[0].message.parsed
        missing = _find_uncited_factual_sentences(parsed.rationale, len(sources))

        if missing:
            raise VerdictCitationError(
                f"Verdict still has uncited factual sentences after one retry: {missing}"
            )

    return Verdict(
        claim_id=claim.claim_id,
        label=parsed.label,
        rationale=parsed.rationale,
        confidence_score=parsed.confidence_score,
        citations=[item.source_url for item in sources],
        created_at=datetime.now(timezone.utc),
    )
