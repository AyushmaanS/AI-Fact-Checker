import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from app.agents.evidence_ranker import EVIDENCE_CAP
from app.citation_parsing import extract_citation_indices, split_sentences
from app.llm_client import MODEL_GPT4O, MODEL_GPT4O_MINI, get_async_llm_client
from app.models.schemas import AnalystOutput, Claim, EvidenceItem, EvidencePackage, Verdict

NO_SOURCES_RATIONALE = "No usable sources were found to verify or refute this claim."

_ASSERTION_VERBS = {
    "is", "are", "was", "were", "has", "have", "had", "confirms", "confirmed",
    "shows", "showed", "states", "stated", "reports", "reported", "found",
    "indicates", "indicated", "according", "occurred", "began", "completed",
    "announced", "declared", "revealed",
}

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

# Stage 2 exists because a keyword-only check can't tell "this introduces a new,
# uncited fact" from "this restates a fact that was already cited a sentence ago."
# A hardcoded list of exempt phrases ("the claim is true/false...") was tried first
# (Sprints 8/9/11) and kept missing new phrasings the model would use instead
# ("is corroborated by", "the evidence supports this", "refuting the claim") - an
# unbounded list of ways to say the same thing. This asks a model to judge the
# actual distinction instead of pattern-matching for it.
STAGE2_SYSTEM_PROMPT = (
    "You classify sentences pulled from a fact-check verdict's rationale. Each "
    "sentence below was flagged because it looks like it states a fact but has no "
    "source citation attached. For EACH numbered sentence, decide: does it assert "
    "a NEW, independently fact-checkable detail - a specific name, date, number, "
    "quote, or event - that a reader would need its own source for? Or is it "
    "evaluative/summary language about the evidence itself (e.g. restating the "
    "verdict, saying the evidence 'confirms', 'supports', or 'corroborates' "
    "something already established elsewhere, describing how strong or consistent "
    "the evidence is) that doesn't introduce any new checkable detail? Mark "
    "is_new_fact=true only for the former - sentences that genuinely need their "
    "own citation."
)


class _VerdictLLMOutput(BaseModel):
    label: Literal[
        "TRUE", "FALSE", "PARTIALLY_TRUE", "MISLEADING", "UNVERIFIABLE", "OUTDATED", "SATIRE"
    ]
    rationale: str
    confidence_score: float


class _SentenceClassification(BaseModel):
    index: int
    is_new_fact: bool


class _SentenceClassificationBatch(BaseModel):
    items: list[_SentenceClassification]


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


def _stage1_heuristic_flag(rationale: str, num_sources: int) -> list[str]:
    """Cheap, deterministic pass: sentences that look factual (a number, a proper
    noun, or an assertion verb) and have no valid [SOURCE_N] tag. Unchanged from
    the original single-stage validator - still has false positives (a restated
    fact "looks" just as factual as a new one), which is exactly what Stage 2 is
    for."""
    problems = []
    for sentence in split_sentences(rationale):
        if not _sentence_looks_factual(sentence):
            continue
        indices = extract_citation_indices(sentence)
        if not any(1 <= i <= num_sources for i in indices):
            problems.append(sentence)
    return problems


async def _stage2_semantic_filter(sentences: list[str]) -> list[str]:
    """Batches every Stage-1-flagged sentence into one gpt-4o-mini call and keeps
    only the ones that actually assert a new, uncited fact."""
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences))
    completion = await get_async_llm_client().chat.completions.parse(
        model=MODEL_GPT4O_MINI,
        messages=[
            {"role": "system", "content": STAGE2_SYSTEM_PROMPT},
            {"role": "user", "content": numbered},
        ],
        response_format=_SentenceClassificationBatch,
    )
    classification = {
        item.index: item.is_new_fact for item in completion.choices[0].message.parsed.items
    }
    # A sentence the model didn't return a classification for defaults to "still
    # flagged" - stricter citation enforcement wins over silently dropping it.
    return [s for i, s in enumerate(sentences) if classification.get(i, True)]


async def _find_uncited_factual_sentences(rationale: str, num_sources: int) -> list[str]:
    stage1_flagged = _stage1_heuristic_flag(rationale, num_sources)
    if not stage1_flagged:
        return []
    return await _stage2_semantic_filter(stage1_flagged)


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
    missing = await _find_uncited_factual_sentences(parsed.rationale, len(sources))

    if missing:
        # Everything in `missing` has already survived Stage 2's semantic filter,
        # so - unlike the old single-stage message - this doesn't need to hedge
        # with "if this is just a restatement": every item here genuinely needs
        # its own citation.
        correction = (
            "Your rationale had factual statement(s) with no valid [SOURCE_N] citation:\n"
            + "\n".join(f'- "{s}"' for s in missing)
            + "\n\nRewrite the ENTIRE rationale so every one of these facts cites a source "
            f"between [SOURCE_1] and [SOURCE_{len(sources)}], cited individually (e.g. "
            "[SOURCE_1] [SOURCE_2], never combined in one bracket). Do not invent new sources."
        )
        messages.append({"role": "assistant", "content": parsed.rationale})
        messages.append({"role": "user", "content": correction})

        completion = await client.chat.completions.parse(
            model=MODEL_GPT4O, messages=messages, response_format=_VerdictLLMOutput
        )
        parsed = completion.choices[0].message.parsed
        missing = await _find_uncited_factual_sentences(parsed.rationale, len(sources))

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
