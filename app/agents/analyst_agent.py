from typing import Optional

from pydantic import BaseModel

from app.llm_client import MODEL_GPT4O, get_async_llm_client
from app.models.schemas import AnalystOutput, Claim, EvidenceItem, EvidencePackage

NO_FOR_EVIDENCE_TEXT = "No supporting evidence found in searched sources."
NO_AGAINST_EVIDENCE_TEXT = "No contradicting evidence found in searched sources."

CREDIBLE_THRESHOLD = 0.75
CONSENSUS_THRESHOLD = 0.9

ANALYST_SYSTEM_PROMPT = (
    "You are an adversarial fact-checking analyst. Given a claim and evidence already "
    "split into supporting (\"for\") and contradicting (\"against\") groups, write two "
    "short, neutral summaries:\n"
    "- for_summary: what the supporting evidence says.\n"
    "- against_summary: what the contradicting evidence says.\n"
    "Only summarize the evidence you are given for each side - never invent evidence "
    "for a side that has none.\n"
    "Also set outdated_flag: true only if the claim describes a CURRENT status, "
    "ranking, or measurement that can change over time (e.g. \"the tallest building "
    "in the world\", \"the current population of X\", \"the reigning champion\") AND "
    "the evidence suggests that status has since changed. A completed historical "
    "event (when something was built, when something happened) is NEVER outdated "
    "merely because it has a date attached - historical facts do not expire. Default "
    "to false unless the evidence specifically shows the claim's status has changed."
)


class _AnalystLLMOutput(BaseModel):
    for_summary: str
    against_summary: str
    outdated_flag: bool


def _build_evidence_block(items: list[EvidenceItem]) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"- [{item.credibility_weight:.2f}] {item.excerpt}" for item in items)


def _detect_fringe_vs_consensus(
    evidence_for: list[EvidenceItem], evidence_against: list[EvidenceItem]
) -> Optional[str]:
    tagged = [(item, "for") for item in evidence_for] + [(item, "against") for item in evidence_against]
    credible = [(item, stance) for item, stance in tagged if item.credibility_weight >= CREDIBLE_THRESHOLD]
    low_cred = [(item, stance) for item, stance in tagged if item.credibility_weight < CREDIBLE_THRESHOLD]

    if not credible:
        return None

    credible_for = sum(1 for _, stance in credible if stance == "for")
    majority_stance = "for" if credible_for >= len(credible) - credible_for else "against"
    majority_share = max(credible_for, len(credible) - credible_for) / len(credible)

    if majority_share < CONSENSUS_THRESHOLD:
        return None

    minority_stance = "against" if majority_stance == "for" else "for"
    if not any(stance == minority_stance for _, stance in low_cred):
        return None

    verdict_word = "supported" if majority_stance == "for" else "disputed"
    return (
        f"{majority_share:.0%} of credible sources (weight >= {CREDIBLE_THRESHOLD}) agree the "
        f"claim is {verdict_word}, while lower-credibility sources push back against that consensus."
    )


async def analyze_evidence(claim: Claim, evidence: EvidencePackage) -> AnalystOutput:
    if not evidence.evidence_for and not evidence.evidence_against:
        return AnalystOutput(
            claim_id=claim.claim_id,
            for_summary=NO_FOR_EVIDENCE_TEXT,
            against_summary=NO_AGAINST_EVIDENCE_TEXT,
            fringe_vs_consensus_note=None,
            outdated_flag=False,
        )

    user_content = (
        f"Claim: {claim.text}\n\n"
        f"Supporting evidence:\n{_build_evidence_block(evidence.evidence_for)}\n\n"
        f"Contradicting evidence:\n{_build_evidence_block(evidence.evidence_against)}"
    )

    completion = await get_async_llm_client().chat.completions.parse(
        model=MODEL_GPT4O,
        messages=[
            {"role": "system", "content": ANALYST_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format=_AnalystLLMOutput,
    )
    parsed = completion.choices[0].message.parsed

    # Never trust the model alone for the "must not be blank" guarantee - enforce it here.
    for_summary = parsed.for_summary if evidence.evidence_for else NO_FOR_EVIDENCE_TEXT
    against_summary = parsed.against_summary if evidence.evidence_against else NO_AGAINST_EVIDENCE_TEXT

    return AnalystOutput(
        claim_id=claim.claim_id,
        for_summary=for_summary,
        against_summary=against_summary,
        fringe_vs_consensus_note=_detect_fringe_vs_consensus(
            evidence.evidence_for, evidence.evidence_against
        ),
        outdated_flag=parsed.outdated_flag,
    )
