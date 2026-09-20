import re
from datetime import datetime, timezone

from app.llm_client import MODEL_GPT4O, get_async_llm_client
from app.models.schemas import (
    AnalystOutput,
    Claim,
    EvidenceItem,
    EvidenceLine,
    EvidencePackage,
    Verdict,
    VerdictAgentOutput,
)

NO_SOURCES_RATIONALE = "No usable sources were found to verify or refute this claim."

AUDIT_FALLBACK_SUMMARY = (
    "A verdict could not be produced without introducing a detail beyond the "
    "sourced evidence below, so only that evidence is being reported."
)

VERDICT_SYSTEM_PROMPT = (
    "You are a fact-checking verdict writer. You will be shown a claim and a "
    "numbered list of evidence lines - each one a paraphrased fact already tied to "
    "a specific source. Your job:\n\n"
    "1. Choose exactly one label:\n"
    "- TRUE: evidence clearly supports the claim, no credible contradiction.\n"
    "- FALSE: evidence clearly contradicts the claim.\n"
    "- PARTIALLY_TRUE: the claim is accurate in part but omits or misstates something material.\n"
    "- MISLEADING: technically defensible but creates a false impression (e.g. true fact, wrong context).\n"
    "- UNVERIFIABLE: the evidence is too thin or inconclusive to decide either way.\n"
    "- OUTDATED: was accurate at some point but the evidence shows the situation has since changed.\n"
    "- SATIRE: the claim is satire or comedy, not a genuine factual assertion.\n\n"
    "2. Write summary_line: exactly one closing sentence that reasons over the "
    "evidence lines shown and states your verdict. It must NOT introduce any new "
    "named entity, date, or number beyond what's already in the evidence lines "
    "above - it may only weigh, connect, or draw a conclusion from what's already "
    "there. If your reasoning depends on a fact not shown in the evidence lines, "
    "leave it out of summary_line entirely.\n\n"
    "3. Set confidence_score (0.0-1.0) reflecting how confident you are in the "
    "label given the evidence balance you were shown."
)


def _resolve_selected_items(ids: list[str], items: list[EvidenceItem]) -> list[EvidenceItem]:
    by_id = {item.evidence_id: item for item in items}
    return [by_id[i] for i in ids if i in by_id]


def _to_evidence_lines(items: list[EvidenceItem]) -> list[EvidenceLine]:
    return [
        EvidenceLine(
            paraphrase=item.paraphrase,
            citation=item.source_url,
            stance=item.stance,
            credibility_weight=item.credibility_weight,
        )
        for item in items
    ]


def _build_evidence_lines(analyst: AnalystOutput, evidence: EvidencePackage) -> list[EvidenceLine]:
    """Code only, no LLM call - resolves the Analyst Agent's selected ids back into
    EvidenceItems and turns each into an EvidenceLine. This is the whole reason the
    Verdict Agent's LLM call never sees a raw URL to get wrong: it only ever sees
    the numbered, pre-built lines below."""
    for_items = _resolve_selected_items(analyst.selected_for_ids, evidence.evidence_for)
    against_items = _resolve_selected_items(analyst.selected_against_ids, evidence.evidence_against)
    return _to_evidence_lines(for_items) + _to_evidence_lines(against_items)


def _format_evidence_lines(lines: list[EvidenceLine]) -> str:
    return "\n".join(
        f"{i}. ({line.stance}, weight {line.credibility_weight:.2f}) {line.paraphrase} "
        f"- URL: {line.citation}"
        for i, line in enumerate(lines, start=1)
    )


# A comma only counts as part of the number when followed by exactly 3 more
# digits (a genuine thousands separator, "13,000") - not [\d,]*, which greedily
# swallows a plain sentence comma after a number. Same reasoning for the
# trailing "." - only "." followed by digits is a decimal.
_NUMBER_PATTERN = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def _extract_factual_details(text: str) -> list[str]:
    """The specific number/date and proper-noun tokens in a piece of text - the
    individual checkable details a sentence is built from, not the sentence as a
    whole. summary_line almost never repeats an evidence line verbatim (it
    paraphrases), so checking the whole sentence for reuse fails even when it's
    legitimately just restating an already-shown detail in new words - checking
    the details themselves survives paraphrasing."""
    details = list(_NUMBER_PATTERN.findall(text))
    words = text.strip().split()
    for i, w in enumerate(words):
        cleaned = w.strip(".,;:!?\"'()")
        if not (cleaned and cleaned[0].isalpha() and cleaned[0].isupper()):
            continue
        # Skip a capitalized word that merely starts a sentence (the first word
        # overall, or the word right after a ".", "!", or "?") - not a proper
        # noun, just normal capitalization.
        starts_sentence = i == 0 or words[i - 1].rstrip("\"')").endswith((".", "!", "?"))
        if starts_sentence:
            continue
        details.append(cleaned)
    return details


def looks_factual(text: str) -> bool:
    """Cheap heuristic: does this string contain a number/date or a proper noun?
    Used only to audit summary_line for a smuggled-in new fact."""
    return bool(_extract_factual_details(text))


def audit_summary_line(summary_line: str, evidence_lines: list[EvidenceLine]) -> list[str]:
    """Returns the factual details in summary_line that don't appear in any
    evidence line's paraphrase - i.e. new facts introduced beyond what the model
    was shown. Empty list means summary_line is clean."""
    evidence_text = " ".join(line.paraphrase for line in evidence_lines)
    details = _extract_factual_details(summary_line)
    return [detail for detail in details if detail not in evidence_text]


def _build_user_content(
    claim: Claim, evidence: EvidencePackage, analyst: AnalystOutput, evidence_lines: list[EvidenceLine]
) -> str:
    return (
        f"Claim: {claim.text}\n\n"
        f"Evidence balance score (credibility-weighted, 0=all against, 1=all for): "
        f"{evidence.confidence_raw:.2f}\n"
        f"Outdated flag (analyst's judgment): {analyst.outdated_flag}\n\n"
        f"Evidence lines:\n{_format_evidence_lines(evidence_lines)}"
    )


def _audit_correction_message(problems: list[str]) -> str:
    listed = "\n".join(f'- "{p}"' for p in problems)
    return (
        f"Your summary_line introduces detail(s) not present in any evidence line "
        f"shown to you:\n{listed}\n\n"
        "Rewrite summary_line so it only reasons over facts already stated in the "
        "evidence lines above, introducing no new checkable detail of its own."
    )


async def produce_verdict(claim: Claim, analyst: AnalystOutput, evidence: EvidencePackage) -> Verdict:
    evidence_lines = _build_evidence_lines(analyst, evidence)

    if not evidence_lines:
        return Verdict(
            claim_id=claim.claim_id,
            label="UNVERIFIABLE",
            evidence_lines=[],
            summary_line=NO_SOURCES_RATIONALE,
            confidence_score=0.0,
            citations=[],
            created_at=datetime.now(timezone.utc),
        )

    citations = list(dict.fromkeys(line.citation for line in evidence_lines))
    client = get_async_llm_client()
    messages = [
        {"role": "system", "content": VERDICT_SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_content(claim, evidence, analyst, evidence_lines)},
    ]

    completion = await client.chat.completions.parse(
        model=MODEL_GPT4O, messages=messages, response_format=VerdictAgentOutput
    )
    parsed = completion.choices[0].message.parsed
    problems = audit_summary_line(parsed.summary_line, evidence_lines)

    if problems:
        messages.append({"role": "assistant", "content": parsed.summary_line})
        messages.append({"role": "user", "content": _audit_correction_message(problems)})

        completion = await client.chat.completions.parse(
            model=MODEL_GPT4O, messages=messages, response_format=VerdictAgentOutput
        )
        parsed = completion.choices[0].message.parsed
        problems = audit_summary_line(parsed.summary_line, evidence_lines)

        if problems:
            # Unlike the old design's VerdictCitationError (raised up to the caller,
            # which then discarded everything), the real evidence_lines/citations
            # are kept here - only the untrustworthy summary_line is swapped out.
            return Verdict(
                claim_id=claim.claim_id,
                label="UNVERIFIABLE",
                evidence_lines=evidence_lines,
                summary_line=AUDIT_FALLBACK_SUMMARY,
                confidence_score=0.0,
                citations=citations,
                created_at=datetime.now(timezone.utc),
            )

    return Verdict(
        claim_id=claim.claim_id,
        label=parsed.label,
        evidence_lines=evidence_lines,
        summary_line=parsed.summary_line,
        confidence_score=parsed.confidence_score,
        citations=citations,
        created_at=datetime.now(timezone.utc),
    )
