# Functional Spec — Phase 1 & Phase 2
**Derived from:** `prd-v1-draft.md`, Phases 1–2 only · **Status:** Ready for build

This spec turns PRD v1's Phase 1 and Phase 2 requirements into exact behavior: data shapes, agent I/O contracts, API endpoints, and edge cases. It's meant to be self-contained enough to hand to Claude one sprint at a time without re-explaining the whole PRD each time.

---

## Out of Scope (repeated from PRD v1, scoped to these two phases)

- No Instagram DM integration (that's Phase 3) — Phase 2's "Media Downloader" only needs the headless-browser/direct-upload paths, **not** the Meta `media_url` strategy (that only exists once a DM webhook payload is available in Phase 3).
- No claim deduplication, no Redis, no vector DB, no job queue — everything in Phase 1–2 runs synchronously, request-in/response-out.
- No media-diet profiling, no web dashboard beyond a raw JSON/simple HTML response.
- No production reliability engineering (circuit breakers, 5-level degradation) — basic try/retry only.

---

## Part A — Phase 1: Core Verification Engine

### A.1 User Flow

1. User submits raw text (a claim, a caption, a paragraph) via a web form or API call.
2. System classifies intent → if not a factual claim, returns early with an appropriate message.
3. System extracts one or more atomic claims.
4. For each claim (in parallel), a Research Agent gathers evidence.
5. Analyst Agent synthesizes for/against evidence per claim.
6. Verdict Agent produces a cited verdict per claim (+ an aggregate verdict if multiple claims).
7. Citation Verifier checks every citation is real and on-topic.
8. Response Formatter returns the final structured result.

### A.2 Data Models

```python
from pydantic import BaseModel
from typing import Literal, Optional
from datetime import datetime

class ContentIntent(BaseModel):
    label: Literal["FACTUAL_CLAIM", "OPINION", "SATIRE_COMEDY", "FICTIONAL_CREATIVE", "UNRELATED"]
    confidence: float

class Claim(BaseModel):
    claim_id: str
    text: str
    topic: str
    specificity: Literal["specific", "general"]
    verifiability_score: float  # 0.0–1.0

class EvidenceItem(BaseModel):
    source_url: str
    source_category: str        # one of the 8 credibility tiers
    credibility_weight: float   # 0.10–0.95, from the seeded table
    excerpt: str
    stance: Literal["for", "against"]
    published_date: Optional[str] = None

class EvidencePackage(BaseModel):
    claim_id: str
    evidence_for: list[EvidenceItem]
    evidence_against: list[EvidenceItem]
    sources: list[str]
    confidence_raw: float        # capped weighted-evidence-score — see A.3 note below

class AnalystOutput(BaseModel):
    claim_id: str
    for_summary: str
    against_summary: str        # MUST be populated — see A.5
    outdated_flag: bool = False

class Verdict(BaseModel):
    claim_id: str
    label: Literal["TRUE", "FALSE", "PARTIALLY_TRUE", "MISLEADING", "UNVERIFIABLE", "OUTDATED", "SATIRE"]
    rationale: str
    confidence_score: float
    citations: list[str]        # every factual sentence in `rationale` must map to one of these
    created_at: datetime

class VerifyResponse(BaseModel):
    submission_id: str
    claims: list[Claim]
    verdicts: list[Verdict]
    aggregate_label: Optional[str] = None
    processing_time_ms: int
```

> ⚠️ **Known open question, inherited from PRD v1:** the source article's prose references a `DISPUTED` verdict (for genuinely 50/50-split evidence) but the canonical taxonomy table only has the 7 labels above. This spec implements the **7-label taxonomy exactly as PRD v1's appendix specifies**. If you want `DISPUTED` as an 8th label, decide before Sprint 8 (Verdict Agent) — changing the label set later means re-touching the prompt, the schema, and the eval set.

### A.3 Agent-by-Agent Behavior

| Agent | Input | Output | Notes |
|---|---|---|---|
| **Content Intent Classifier** | raw text | `ContentIntent` | Only `FACTUAL_CLAIM` proceeds. All others short-circuit with a canned response (e.g., `OPINION` → "This reads as opinion, not a checkable claim."). |
| **Claim Extractor** | raw text | `list[Claim]` | Must handle 0 claims (return empty list → API responds "no verifiable claims found"), 1 claim, and multi-claim paragraphs. |
| **Research Agent** (×N, parallel) | one `Claim` | `EvidencePackage` | Internally: Query Decomposer (3–5 search queries) → Tavily search calls → raw results. Capped at 8 concurrent; >8 claims batch in groups of 8. |
| **Evidence Ranker** | raw search results + `Claim` | `EvidencePackage` (scored) | Applies the credibility-weight table; sorts by credibility × relevance × recency. Also computes `confidence_raw` as a **capped weighted-evidence-score**: take each side's top `EVIDENCE_CAP` (5) items by `credibility_weight`, sum each side's weights, `confidence_raw = for_score / (for_score + against_score)` (0.5 if there's no evidence at all). The cap exists so a flood of low-credibility sources can't outvote a few highly-credible ones (e.g. 2 sources at weight ≥0.90 against a claim correctly outweighs 15 sources at weight 0.10–0.30 for it) — see `evidence_ranker.compute_evidence_score`. |
| **Analyst Agent** | `Claim` + `EvidencePackage` | `AnalystOutput` | `against_summary` is **never blank** — if no contradicting evidence exists, it must literally contain "No contradicting evidence found in searched sources" (and symmetrically for `for_summary`). Evidence-balance scoring (the old fringe-vs-consensus threshold check) has moved entirely to the Evidence Ranker's `confidence_raw` — the Analyst Agent's job is strictly the two summaries plus `outdated_flag`, nothing about evidence weighting. |
| **Verdict Agent** | `Claim` + `AnalystOutput` | `Verdict` | Citation-enforced: every sentence asserting a fact must reference a citation index. A schema validator rejects/retries output where a factual sentence has no citation. |
| **Citation Verifier** | `Verdict.citations` | adjusted `Verdict` | Fetches each URL, text-searches for the attributed claim. Missing/mismatched → citation removed, `confidence_score` reduced proportionally. |
| **Response Formatter** | final `Verdict[]` | `VerifyResponse` | Assembles the API response; computes `aggregate_label` when >1 claim (simple rule: worst-case label wins, e.g., any FALSE claim makes the aggregate at least PARTIALLY_TRUE). |

### A.4 API Contract (Phase 1)

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/verify` | POST | `{ "text": str }` | `VerifyResponse` (synchronous — Phase 1 has no queue, so this call blocks for the ~30–90s pipeline duration) |
| `/verdicts/{submission_id}` | GET | — | previously computed `VerifyResponse` |
| `/health` | GET | — | `{ "status": "ok" }` |

### A.5 Edge Cases & Definition of Done

- Empty/whitespace-only input → 400 error, no pipeline run.
- Input with zero extractable claims → 200 response, empty `verdicts[]`, explanatory message.
- A claim where research returns no usable sources at all → `UNVERIFIABLE`, not a fabricated verdict.
- A claim where the Analyst finds only supporting evidence → `against_summary` still explicitly states none was found (never omitted).
- Citation Verifier catches at least one deliberately-planted mismatched citation in testing.
- **Phase 1 is done when:** a real, previously-unseen claim submitted via `POST /verify` returns a correctly-labeled, fully-cited `VerifyResponse` in under ~90 seconds, for claims spanning at least 4 of the 7 verdict labels in manual testing.

---

## Part B — Phase 2: Content Ingestion Pipeline

### B.1 User Flow

1. User either (a) uploads a video file directly, or (b) pastes a public Reel/post URL.
2. System resolves/normalizes the URL (if applicable) and downloads the media (with fallback strategies).
3. Media Router determines type: video / image / text-post.
4. Video path: extract audio → transcribe → sample frames → vision analysis. Caption path: extract caption + hashtags/links.
5. All paths converge on one `StructuredContentObject`.
6. That object is handed to the Phase 1 engine exactly as if it were pasted text — **no changes to Phase 1 code required.**

### B.2 Data Models

```python
class StructuredContentObject(BaseModel):
    transcript: Optional[str] = None
    visual_context: Optional[str] = None   # GPT-4o Vision frame description + OCR text
    caption: Optional[str] = None
    source_url: Optional[str] = None
    topics: list[str] = []
    language: str = "en"
    media_type: Literal["video", "image", "text_post"]
    low_confidence_transcript: bool = False  # true if Whisper confidence < 0.6
```

### B.3 Component Behavior

| Component | Input | Output | Notes |
|---|---|---|---|
| **URL Resolver** | raw URL string | canonical URL + detected platform | Strips tracking params, resolves shortlinks. |
| **Media Downloader** | canonical URL | local video/image file, or failure | **Phase 2 strategy order:** (1) `yt-dlp` direct download, (2) headless-browser extraction fallback. *(Meta `media_url` strategy is Phase 3-only — not implemented here.)* On total failure → fall back to caption-only if any caption text is available; otherwise return a clear "couldn't download" error. |
| **Media Router** | downloaded file | routes to Video Path or Caption Path | Detects video vs. static image vs. text-only post. |
| **Video Path** | video file | transcript + visual_context | ffmpeg extracts audio → Whisper transcribes → flag `low_confidence_transcript` if Whisper confidence < 0.6 → ffmpeg samples 1 frame/sec → GPT-4o Vision describes frames + OCRs on-screen text. |
| **Caption Path** | post metadata/text | caption + topics | Extracts caption text; pulls hashtags/links as topic-context signals. |

### B.4 API Contract Additions (Phase 2)

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/verify/upload` | POST | multipart file | `VerifyResponse` (runs ingestion, then the full Phase 1 pipeline) |
| `/verify/url` | POST | `{ "url": str }` | `VerifyResponse` (same) |

### B.5 Edge Cases & Definition of Done

- Music-only video (no speech) → transcript is empty/low-confidence; pipeline still proceeds using `visual_context` + caption.
- Static image with on-screen text, no video → `media_type: "image"`, transcript is `null`, `visual_context` carries the OCR result.
- Private/deleted/broken URL → graceful caption-only fallback or a clear user-facing error — **never a raw stack trace.**
- Every input type produces a `StructuredContentObject` with the **same shape** (this is the hard contract with Phase 1 — verify with a schema test across all three media types).
- **Phase 2 is done when:** a real public Instagram Reel URL, pasted into `POST /verify/url`, produces a full cited verdict end-to-end with no manual intervention.
