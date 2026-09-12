from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class ContentIntent(BaseModel):
    label: Literal["FACTUAL_CLAIM", "OPINION", "SATIRE_COMEDY", "FICTIONAL_CREATIVE", "UNRELATED"]
    confidence: float


class Claim(BaseModel):
    claim_id: str
    text: str
    topic: str
    specificity: Literal["specific", "general"]
    verifiability_score: float  # 0.0-1.0


class EvidenceItem(BaseModel):
    source_url: str
    source_category: str  # one of the 8 credibility tiers
    credibility_weight: float  # 0.10-0.95, from the seeded table
    excerpt: str
    stance: Literal["for", "against"]
    published_date: Optional[str] = None


class EvidencePackage(BaseModel):
    claim_id: str
    evidence_for: list[EvidenceItem]
    evidence_against: list[EvidenceItem]
    sources: list[str]
    confidence_raw: float


class AnalystOutput(BaseModel):
    claim_id: str
    for_summary: str
    against_summary: str  # MUST be populated - see functional spec A.5
    fringe_vs_consensus_note: Optional[str] = None
    outdated_flag: bool = False


class Verdict(BaseModel):
    claim_id: str
    label: Literal["TRUE", "FALSE", "PARTIALLY_TRUE", "MISLEADING", "UNVERIFIABLE", "OUTDATED", "SATIRE"]
    rationale: str
    confidence_score: float
    citations: list[str]  # every factual sentence in `rationale` must map to one of these
    created_at: datetime


class VerifyResponse(BaseModel):
    submission_id: str
    claims: list[Claim]
    verdicts: list[Verdict]
    aggregate_label: Optional[str] = None
    processing_time_ms: int
