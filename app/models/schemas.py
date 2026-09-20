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
    evidence_id: str
    source_url: str
    source_category: str  # one of the 8 credibility tiers
    credibility_weight: float  # 0.10-0.95, from the seeded table
    excerpt: str
    paraphrase: str  # one-line paraphrase, generated once here while still tied to a fixed source
    stance: Literal["for", "against"]
    published_date: Optional[str] = None


class EvidencePackage(BaseModel):
    claim_id: str
    evidence_for: list[EvidenceItem]
    evidence_against: list[EvidenceItem]
    sources: list[str]
    # Capped weighted-evidence-score: sum(credibility_weight) over each side's top
    # EVIDENCE_CAP items, then for_score / (for_score + against_score) - 0.5 if
    # there's no evidence at all. See evidence_ranker.compute_evidence_score.
    confidence_raw: float


class AnalystOutput(BaseModel):
    claim_id: str
    # Plain-code selection (top 3 per side by credibility_weight) - see
    # analyst_agent._select_top_ids. Not an LLM judgment call.
    selected_for_ids: list[str]
    selected_against_ids: list[str]
    outdated_flag: bool = False


class EvidenceLine(BaseModel):
    """A single piece of evidence, assembled entirely from code (see
    verdict_agent._build_evidence_lines) from an EvidenceItem the Analyst Agent
    selected - never produced by an LLM call."""

    paraphrase: str
    citation: str
    stance: Literal["for", "against"]
    credibility_weight: float


class VerdictAgentOutput(BaseModel):
    """The only thing the Verdict Agent's LLM call produces - no citations, no
    evidence, nothing that could ever be mismatched against a source."""

    label: Literal["TRUE", "FALSE", "PARTIALLY_TRUE", "MISLEADING", "UNVERIFIABLE", "OUTDATED", "SATIRE"]
    summary_line: str
    confidence_score: float


class Verdict(BaseModel):
    claim_id: str
    label: Literal["TRUE", "FALSE", "PARTIALLY_TRUE", "MISLEADING", "UNVERIFIABLE", "OUTDATED", "SATIRE"]
    evidence_lines: list[EvidenceLine]
    summary_line: str
    confidence_score: float
    citations: list[str]  # always derived: deduplicated evidence_lines[*].citation, never LLM-produced
    created_at: datetime


class VerifyResponse(BaseModel):
    submission_id: str
    claims: list[Claim]
    verdicts: list[Verdict]
    aggregate_label: Optional[str] = None
    processing_time_ms: int
    # Not in the original spec's VerifyResponse - added in Sprint 10 so the
    # non-factual-intent and zero-claim short-circuits (spec A.3/A.5) have
    # somewhere to put their canned explanation without overloading
    # aggregate_label, which is meant to hold a verdict label, not free text.
    message: Optional[str] = None


class StructuredContentObject(BaseModel):
    transcript: Optional[str] = None
    visual_context: Optional[str] = None  # GPT-4o Vision frame description + OCR text
    caption: Optional[str] = None
    source_url: Optional[str] = None
    topics: list[str] = []
    language: str = "en"
    media_type: Literal["video", "image", "text_post"]
    low_confidence_transcript: bool = False  # true if Whisper confidence < 0.6
