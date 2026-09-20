from pydantic import BaseModel

from app.llm_client import MODEL_GPT4O, get_async_llm_client
from app.models.schemas import AnalystOutput, Claim, EvidenceItem, EvidencePackage

SELECTED_EVIDENCE_CAP = 3

ANALYST_SYSTEM_PROMPT = (
    "You are a fact-checking analyst. You will be shown a claim and all evidence "
    "gathered for it, split into supporting (\"for\") and contradicting (\"against\") "
    "groups. Your only job is to set outdated_flag: true only if the claim describes "
    "a CURRENT status, ranking, or measurement that can change over time (e.g. \"the "
    "tallest building in the world\", \"the current population of X\", \"the reigning "
    "champion\") AND the evidence suggests that status has since changed. A completed "
    "historical event (when something was built, when something happened) is NEVER "
    "outdated merely because it has a date attached - historical facts do not expire. "
    "Default to false unless the evidence specifically shows the claim's status has "
    "changed."
)


class _AnalystLLMOutput(BaseModel):
    outdated_flag: bool


def _build_evidence_block(items: list[EvidenceItem]) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"- [{item.credibility_weight:.2f}] {item.excerpt}" for item in items)


def _select_top_ids(items: list[EvidenceItem]) -> list[str]:
    # Plain code, no LLM call - sorts independently rather than trusting the
    # Evidence Ranker's own sort order, so this stays correct even if that
    # upstream ordering ever changes. An empty side just yields an empty list.
    top = sorted(items, key=lambda e: e.credibility_weight, reverse=True)[:SELECTED_EVIDENCE_CAP]
    return [item.evidence_id for item in top]


async def analyze_evidence(claim: Claim, evidence: EvidencePackage) -> AnalystOutput:
    selected_for_ids = _select_top_ids(evidence.evidence_for)
    selected_against_ids = _select_top_ids(evidence.evidence_against)

    if not evidence.evidence_for and not evidence.evidence_against:
        return AnalystOutput(
            claim_id=claim.claim_id,
            selected_for_ids=selected_for_ids,
            selected_against_ids=selected_against_ids,
            outdated_flag=False,
        )

    # outdated_flag looks at the FULL package, not just the selected top 3 per
    # side - a recency signal can live in a source that isn't among the most
    # credible ones.
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

    return AnalystOutput(
        claim_id=claim.claim_id,
        selected_for_ids=selected_for_ids,
        selected_against_ids=selected_against_ids,
        outdated_flag=parsed.outdated_flag,
    )
