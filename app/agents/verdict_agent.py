import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError

from app.agents.evidence_ranker import EVIDENCE_CAP
from app.llm_client import MODEL_GPT4O, get_async_llm_client
from app.models.schemas import (
    AnalystOutput,
    Claim,
    EvidenceItem,
    EvidencePackage,
    RationaleSegment,
    Verdict,
)

NO_SOURCES_RATIONALE = "No usable sources were found to verify or refute this claim."

VERDICT_SYSTEM_PROMPT = (
    "You are a fact-checking verdict writer. Given a claim, an adversarial analyst's "
    "summary of supporting and contradicting evidence, and a numbered list of sources, "
    "write a verdict as a sequence of rationale segments, each one of two types:\n\n"
    "- sourced_fact: a sentence asserting a specific, checkable detail (a date, a "
    "number, a name, a quote, an event) that came from one of the sources. Set "
    "`citation` to the EXACT URL of the source it came from - copy it verbatim from "
    "the numbered list below. Never invent a URL.\n"
    "- connective_reasoning: a sentence that reasons over, summarizes, or draws a "
    "conclusion from facts already stated in your sourced_fact segments - including "
    "your closing verdict statement (e.g. \"this confirms the claim is false\"). "
    "Leave `citation` unset (null) for these. A connective_reasoning segment must "
    "NOT introduce any new checkable detail that isn't already covered by a "
    "sourced_fact segment - if it needs a new fact, that fact belongs in its own "
    "sourced_fact segment instead.\n\n"
    "Every specific, checkable detail in your rationale must appear in a sourced_fact "
    "segment with a real citation - never fold a new fact into a connective_reasoning "
    "segment to avoid citing it.\n\n"
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
    rationale_segments: list[RationaleSegment]
    confidence_score: float


class VerdictCitationError(RuntimeError):
    """Raised when the model still produces an invalid or under-cited verdict
    after one retry (schema validation failure, or a connective_reasoning
    segment that smuggles in an uncited new fact)."""


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
        f"{i}. ({item.stance}, weight {item.credibility_weight:.2f}) "
        f"{item.excerpt} - URL: {item.source_url}"
        for i, item in enumerate(sources, start=1)
    )


# A comma only counts as part of the number when followed by exactly 3 more
# digits (a genuine thousands separator, "13,000") - not [\d,]*, which greedily
# swallowed a plain sentence comma after a year ("1889, this..." -> wrongly
# extracted "1889," instead of "1889", which then failed to match the same year
# written as "1889." elsewhere - a real bug caught by live testing). Same
# reasoning for the trailing "." - only "." followed by digits is a decimal.
_NUMBER_PATTERN = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def _extract_factual_details(text: str) -> list[str]:
    """The specific number/date and proper-noun tokens in a piece of text - the
    individual checkable details a sentence is built from, not the sentence as a
    whole. A connective sentence almost never repeats an earlier sentence
    verbatim (it paraphrases), so checking the whole sentence for reuse fails
    even when it's legitimately just restating an already-cited detail in new
    words - checking the details themselves survives paraphrasing."""
    details = list(_NUMBER_PATTERN.findall(text))
    words = text.strip().split()
    for i, w in enumerate(words):
        cleaned = w.strip(".,;:!?\"'()")
        if not (cleaned and cleaned[0].isalpha() and cleaned[0].isupper()):
            continue
        # Skip a capitalized word that merely starts a sentence (the first word
        # overall, or the word right after a ".", "!", or "?") - not a proper
        # noun, just normal capitalization. A segment can contain more than one
        # sentence, so this checks each sentence start, not just word index 0.
        starts_sentence = i == 0 or words[i - 1].rstrip("\"')").endswith((".", "!", "?"))
        if starts_sentence:
            continue
        details.append(cleaned)
    return details


def looks_factual(text: str) -> bool:
    """Cheap heuristic: does this string contain a number/date or a proper noun?
    Used only to audit connective_reasoning segments for smuggled-in new facts -
    NOT a restoration of the old Stage 1 sentence scanner, which ran over a whole
    free-text paragraph and also checked for assertion verbs. Segment typing is
    now self-declared by the model, so that broader scan is no longer needed;
    this just catches a connective segment that looks like it's hiding a fact."""
    return bool(_extract_factual_details(text))


def audit_connective_segments(segments: list[RationaleSegment]) -> list[str]:
    sourced_text = " ".join(s.text for s in segments if s.segment_type == "sourced_fact")
    problems = []
    for segment in segments:
        if segment.segment_type != "connective_reasoning":
            continue
        details = _extract_factual_details(segment.text)
        if not details:
            continue
        if any(detail not in sourced_text for detail in details):
            problems.append(segment.text)
    return problems


def _segments_to_text(segments: list[RationaleSegment]) -> str:
    lines = []
    for s in segments:
        tag = f" [citation: {s.citation}]" if s.citation else ""
        lines.append(f"({s.segment_type}) {s.text}{tag}")
    return "\n".join(lines)


def _try_build_verdict(
    claim: Claim, parsed: _VerdictLLMOutput, citation_urls: list[str]
) -> tuple[Optional[Verdict], Optional[ValidationError]]:
    try:
        verdict = Verdict(
            claim_id=claim.claim_id,
            label=parsed.label,
            rationale_segments=parsed.rationale_segments,
            confidence_score=parsed.confidence_score,
            citations=citation_urls,
            created_at=datetime.now(timezone.utc),
        )
    except ValidationError as exc:
        return None, exc
    return verdict, None


def _schema_correction_message(exc: ValidationError) -> str:
    return (
        f"Your rationale_segments failed validation:\n{exc}\n\n"
        "Every sourced_fact segment must have `citation` set to one of the exact "
        "URLs from the numbered source list. Rewrite the ENTIRE set of "
        "rationale_segments so every sourced_fact segment has a valid citation."
    )


def _audit_correction_message(problems: list[str]) -> str:
    listed = "\n".join(f'- "{p}"' for p in problems)
    return (
        "These connective_reasoning segment(s) look like they introduce a new, "
        f"checkable detail with no citation:\n{listed}\n\n"
        "Rewrite the ENTIRE set of rationale_segments: either turn each one into a "
        "sourced_fact segment with a real citation from the source list, or rephrase "
        "it to only reason over facts already stated in your sourced_fact segments, "
        "introducing no new checkable detail of its own."
    )


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
            rationale_segments=[
                RationaleSegment(text=NO_SOURCES_RATIONALE, segment_type="connective_reasoning")
            ],
            confidence_score=0.0,
            citations=[],
            created_at=datetime.now(timezone.utc),
        )

    citation_urls = [item.source_url for item in sources]
    client = get_async_llm_client()
    messages = [
        {"role": "system", "content": VERDICT_SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_content(claim, analyst, evidence, sources)},
    ]

    completion = await client.chat.completions.parse(
        model=MODEL_GPT4O, messages=messages, response_format=_VerdictLLMOutput
    )
    parsed = completion.choices[0].message.parsed
    verdict, schema_error = _try_build_verdict(claim, parsed, citation_urls)
    audit_problems = audit_connective_segments(parsed.rationale_segments) if verdict else []

    if schema_error or audit_problems:
        correction = (
            _schema_correction_message(schema_error) if schema_error
            else _audit_correction_message(audit_problems)
        )
        messages.append({"role": "assistant", "content": _segments_to_text(parsed.rationale_segments)})
        messages.append({"role": "user", "content": correction})

        completion = await client.chat.completions.parse(
            model=MODEL_GPT4O, messages=messages, response_format=_VerdictLLMOutput
        )
        parsed = completion.choices[0].message.parsed
        verdict, schema_error = _try_build_verdict(claim, parsed, citation_urls)
        audit_problems = audit_connective_segments(parsed.rationale_segments) if verdict else []

        if schema_error or audit_problems:
            raise VerdictCitationError(
                f"Verdict still invalid after one retry - schema_error={schema_error!r}, "
                f"audit_problems={audit_problems}"
            )

    return verdict
