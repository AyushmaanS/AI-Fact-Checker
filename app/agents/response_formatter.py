from typing import Optional

from app.models.schemas import Claim, Verdict, VerifyResponse

# Best-to-worst ordering used when no FALSE/MISLEADING claim is present at all.
_SEVERITY_ORDER = ["TRUE", "SATIRE", "OUTDATED", "UNVERIFIABLE", "PARTIALLY_TRUE", "MISLEADING", "FALSE"]

# Per the sprint spec: FALSE and MISLEADING are "equally severe" for aggregation,
# and a single FALSE/MISLEADING claim mixed among others pulls the aggregate down
# to "at least PARTIALLY_TRUE" rather than the raw worst label - a submission with
# some true and some false claims is fairly described as partially true, not FALSE
# outright. Only when EVERY claim is FALSE/MISLEADING does the aggregate report
# the worse of those two directly.
_SEVERE_LABELS = {"FALSE", "MISLEADING"}


def compute_aggregate_label(verdicts: list[Verdict]) -> Optional[str]:
    if not verdicts:
        return None

    labels = {v.label for v in verdicts}
    if len(labels) == 1:
        return next(iter(labels))

    if labels <= _SEVERE_LABELS:
        return "FALSE" if "FALSE" in labels else "MISLEADING"

    if labels & _SEVERE_LABELS:
        return "PARTIALLY_TRUE"

    return max(labels, key=_SEVERITY_ORDER.index)


def format_response(
    submission_id: str,
    claims: list[Claim],
    verdicts: list[Verdict],
    processing_time_ms: int,
    message: Optional[str] = None,
) -> VerifyResponse:
    return VerifyResponse(
        submission_id=submission_id,
        claims=claims,
        verdicts=verdicts,
        aggregate_label=compute_aggregate_label(verdicts),
        processing_time_ms=processing_time_ms,
        message=message,
    )
