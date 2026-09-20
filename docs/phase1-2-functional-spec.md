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
| **Evidence Ranker** | raw search results + `Claim` | `EvidencePackage` (scored) | Applies the credibility-weight table; sorts by credibility × relevance × recency. Also generates each `EvidenceItem`'s `evidence_id` (code) and a one-line `paraphrase` of its excerpt (`gpt-4o-mini`, in the same call that already classifies stance) — done here, while the source is still a fixed object, so nothing downstream ever has to re-derive or re-attribute it (see §A.3b). Also computes `confidence_raw` as a **capped weighted-evidence-score**: take each side's top `EVIDENCE_CAP` (5) items by `credibility_weight`, sum each side's weights, `confidence_raw = for_score / (for_score + against_score)` (0.5 if there's no evidence at all). The cap exists so a flood of low-credibility sources can't outvote a few highly-credible ones (e.g. 2 sources at weight ≥0.90 against a claim correctly outweighs 15 sources at weight 0.10–0.30 for it) — see `evidence_ranker.compute_evidence_score`. |
| **Analyst Agent** | `Claim` + `EvidencePackage` | `AnalystOutput` | Selection of `selected_for_ids`/`selected_against_ids` is **plain code, no LLM call** — each side's top 3 `EvidenceItem`s by `credibility_weight` (`analyst_agent._select_top_ids`). The Analyst Agent's only remaining LLM call sets `outdated_flag`, looking at the *full* `EvidencePackage`, not just the selected top 3 (a recency signal can live in a source that isn't among the most credible ones). See §A.3b. |
| **Verdict Agent** | `Claim` + `EvidencePackage` + `AnalystOutput` | `Verdict` | Never sees a raw URL to get wrong: the selected `EvidenceItem`s are resolved into `EvidenceLine`s (paraphrase + citation + stance + weight) entirely in code before the single `gpt-4o` call, which produces only `label` + `summary_line` + `confidence_score` — no citations, no evidence, nothing it could mismatch. `summary_line` is audited so it introduces no new named entity/date/number beyond the evidence lines shown (see §A.3b). |
| **Citation Verifier** | `Verdict.evidence_lines` | adjusted `Verdict` | Fetches each line's citation URL, text-searches for *that line's paraphrase* specifically. Missing/mismatched → the entire `EvidenceLine` is removed (not just its citation string), `citations` recomputed from what remains, `confidence_score` reduced proportionally. `summary_line` is never fetched or verified — exempt by design, since it can only reason over already-verified evidence lines, never assert an independent fact of its own. |
| **Response Formatter** | final `Verdict[]` | `VerifyResponse` | Assembles the API response; computes `aggregate_label` when >1 claim (simple rule: worst-case label wins, e.g., any FALSE claim makes the aggregate at least PARTIALLY_TRUE). |

### A.3a Verdict Agent — Two-Stage Citation Validator *(superseded)*

> ⚠️ **Superseded:** this section described the citation-validation mechanism as it existed before the structured-rationale-segments redesign, and again before *this* redesign. Neither `[SOURCE_N]` tags nor rationale segments exist in the codebase anymore — see §A.3b for the current design. Kept below as the historical record of what Sprint 8 originally built.

The Verdict Agent's citation validator originally used a single heuristic pass, plus a hardcoded list of "exempt" phrases (e.g. "the claim is true/false") meant to stop it wrongly flagging sentences that just restate an already-cited verdict rather than assert a new fact. That exempt-phrase list kept missing new phrasings the model would use instead ("is corroborated by," "the evidence supports this," "refuting the claim") — an unbounded list of ways to say the same thing that no fixed word list can keep up with. It's retired entirely, replaced with a two-stage check:

- **Stage 1 (unchanged heuristic, no LLM call):** scan each sentence in the rationale for a number, a proper noun, or a common assertion verb ("is," "confirms," "shows," etc.) with no valid `[SOURCE_N]` tag pointing into the numbered source list. Cheap, deterministic, and — by itself — prone to false positives, since a sentence *restating* an already-cited fact "looks" exactly as factual as one introducing a new one.
- **Stage 2 (new, `gpt-4o-mini`):** every sentence Stage 1 flags gets batched into a single structured-output call asking, per sentence: does it assert a *new*, independently fact-checkable detail (a name, date, number, quote, or event), or is it evaluative/summary language about the evidence itself that introduces nothing new? Only sentences classified as a new fact stay flagged; the rest are dropped. A sentence the model doesn't return a classification for defaults to staying flagged — stricter enforcement wins over silently dropping something uncertain.

The retry/fallback mechanics are unchanged: run the two-stage check → if anything's still flagged, re-prompt once with a correction → run the two-stage check again → if still flagged, raise (the caller falls back to an explained `UNVERIFIABLE` — see Sprint 10's `pipeline.process_claims`).

**What this does and doesn't change:** this makes the validator *more accurate at telling restated facts from new ones* — it does not loosen what counts as needing a citation. A genuinely new, uncited fact is still caught even with no "trigger" assertion verb at all: `"The tower stands 330 meters tall."` gets flagged by Stage 1 (via the number `330`) and stays flagged by Stage 2 (a new checkable detail, not a restatement) — the standing regression case proving Stage 2 hasn't become too permissive, alongside the positive case that the Eiffel Tower's already-cited completion date, restated in a closing sentence, correctly does *not* need its own citation.

### A.3b Evidence Lines + Audited Summary Line — Current Design

§A.3a's two-stage validator (and the structured-rationale-segments design that replaced it after) both worked by *detecting* a citation-attachment failure after the model had already written free text. This design instead makes that failure mode structurally unreachable: the model is never given a URL, and never writes one.

- **Evidence Ranker (Sprint 5):** while each search result is still a fixed object, generate `evidence_id` (code) and `paraphrase` — a one-line, self-contained restatement of the excerpt — in the same `gpt-4o-mini` call that already classifies stance. This is the only point anything is ever paraphrased from a source.
- **Analyst Agent (Sprint 7):** `selected_for_ids`/`selected_against_ids` — each side's top 3 `EvidenceItem`s by `credibility_weight` — are chosen by plain code (`sorted(..., key=lambda e: e.credibility_weight, reverse=True)[:3]`), not an LLM call. The Analyst Agent's one remaining LLM call sets `outdated_flag` only, looking at the full `EvidencePackage`.
- **Verdict Agent (Sprint 8):** the selected ids are resolved back into `EvidenceItem`s and turned into `EvidenceLine`s (`paraphrase`, `citation`, `stance`, `credibility_weight`) — code only. The one `gpt-4o` call is shown these numbered evidence lines and asked to produce exactly `VerdictAgentOutput`: `label`, `summary_line` (one closing sentence), `confidence_score`. Nothing else — it has no field to put a citation *in*, so it cannot mismatch one.

  `summary_line` is audited with `audit_summary_line`: extract its factual details (numbers/dates/proper nouns, via the same detail-level extraction the earlier segment-based design used — `looks_factual` / `_extract_factual_details`) and check each one appears somewhere in the evidence lines' combined paraphrase text. This is a *detail*-level check, not a whole-sentence one, specifically because a closing sentence almost always paraphrases rather than repeats an evidence line verbatim (e.g. "construction concluded in March 1889" vs. "completed on March 31, 1889" — same detail, different wording). If the audit flags something, re-prompt once with a correction; if it still fails, **fall back to `UNVERIFIABLE` in place** — keeping the real `evidence_lines` and their citations, only swapping in a canned `summary_line`. Unlike §A.3a's design, this never raises up to the caller and never discards real evidence: the worst case is "no closing sentence," not "no verdict at all."
- **Citation Verifier (Sprint 9):** unchanged in spirit, changed in unit — now verifies each `EvidenceLine.paraphrase` against its `citation`, and removes the whole line (not just the URL string) on a mismatch. `summary_line` is never sent to this step; it needs no independent verification because the audit above already guarantees it can't assert anything the evidence lines don't already cover.
- **Response Formatter (Sprint 10):** renders each evidence line (paraphrase + citation) followed by `summary_line` as the closing sentence, instead of joining segments.

**Regression cases carried forward from the two prior designs:** `"The tower stands 330 meters tall."` as a `summary_line` with no matching evidence line must still be flagged (via the number `330`) — the audit hasn't gotten more permissive. The Eiffel Tower's completion date, restated in different words in `summary_line` after already appearing in an evidence line's paraphrase, must **not** be flagged — detail-level matching, not verbatim, must survive paraphrasing.

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
