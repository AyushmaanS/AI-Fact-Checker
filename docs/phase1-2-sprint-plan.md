# Sprint Plan — Phase 1 & Phase 2
**Derived from:** `prd-v1-draft.md`, `phase1-2-functional-spec.md`, `phase1-2-engineering-requirements.md`
**Paced for:** part-time solo building alongside full-time work (~5 hrs/week average)

---

## How to use this doc

1. Save all four planning docs (this one, the spec, the ERD, and `prd-v1-draft.md`) into `docs/` in your repo.
2. Each sprint below is one self-contained chunk. Copy the **"Prompt for Claude"** block as-is (edit the file paths if yours differ) and hand it to Claude Code in your repo — it already has enough context via the referenced docs, so you don't need to re-explain the project each time.
3. Do them in order — later sprints assume earlier ones exist. Each sprint has a concrete Definition of Done you can verify yourself in a couple of minutes.
4. **Suggested cadence:** one sprint per week. If a week gets eaten by your day job, that's fine — there's no hard deadline here. Consider a buffer/rest week after Sprint 6 and after Sprint 11; don't trade sleep for schedule.

**Total: 19 sprints (0–18), ~80 hours, roughly 16–19 weeks at a sustainable pace.**

| # | Sprint | Phase | Est. hours |
|---|---|---|---|
| 0 | Project Setup & Scaffolding | 1 | 3 |
| 1 | Data Models & DB Schema | 1 | 4 |
| 2 | Content Intent Classifier | 1 | 3 |
| 3 | Claim Extractor | 1 | 4 |
| 4 | Research Agent (single claim, Tavily) | 1 | 5 |
| 5 | Evidence Ranker + Credibility Weighting | 1 | 4 |
| 6 | Parallelize Research Agents (asyncio) | 1 | 4 |
| 7 | Analyst Agent | 1 | 4 |
| 8 | Verdict Agent + citation validator | 1 | 5 |
| 9 | Citation Verifier | 1 | 4 |
| 10 | Response Formatter + E2E wiring | 1 | 5 |
| 11 | Eval Set & Testing | 1 | 4 |
| 12 | StructuredContentObject + URL Resolver | 2 | 3 |
| 13 | Upload endpoint + Caption Path | 2 | 4 |
| 14 | Media Downloader (yt-dlp) | 2 | 6 |
| 15 | ffmpeg + Whisper transcription | 2 | 4 |
| 16 | Frame Sampler + GPT-4o Vision | 2 | 5 |
| 17 | Media Router + object assembly | 2 | 4 |
| 18 | Full Phase 2 integration + Storage | 2 | 5 |

---

## PHASE 1 — Core Verification Engine

### Sprint 0 — Project Setup & Scaffolding
**~3 hrs · one short weekend session**

**Goal:** A running FastAPI skeleton with config loading and a health check — the foundation everything else builds on.

**Prompt for Claude:**
> Set up a new Python 3.11 FastAPI project called `fact-checker` using the repo structure and tech stack defined in `docs/phase1-2-engineering-requirements.md` §1–2. Create the folder structure exactly as specified, a `requirements.txt` with FastAPI, Pydantic v2, `python-dotenv`, `openai`, `tavily-python`, `supabase`, and `pytest`. Add `config.py` that loads `OPENAI_API_KEY`, `TAVILY_API_KEY`, `SUPABASE_URL`, and `SUPABASE_KEY` from a `.env` file (create a `.env.example` too, gitignored `.env`). Add a `GET /health` endpoint returning `{"status": "ok"}`. Include a short `README.md` with setup instructions.

**Definition of Done:** `uvicorn app.main:app --reload` runs locally; `GET /health` returns 200.

---

### Sprint 1 — Data Models & DB Schema
**~4 hrs**

**Goal:** All Phase 1 Pydantic models exist and match a live Supabase Postgres schema.

**Prompt for Claude:**
> Using `docs/phase1-2-functional-spec.md` §A.2 as the exact spec, create `app/models/schemas.py` with the `ContentIntent`, `Claim`, `EvidenceItem`, `EvidencePackage`, `AnalystOutput`, `Verdict`, and `VerifyResponse` Pydantic models. Then create `app/db/schema.sql` using `docs/phase1-2-engineering-requirements.md` §3 as the exact DDL, plus a seed insert for the 8-tier source credibility table (weights from the PRD: Primary Gov/IGO 0.95, Peer-Reviewed Academic 0.90, Established Fact-Checkers 0.88, Major Wire Services 0.82, Major National Newspapers 0.75, Wikipedia 0.55, Blogs/Opinion 0.30, Social Media 0.10). Create `app/db/client.py` with a Supabase client wrapper and basic insert/query helper functions for the `submissions`, `claims`, and `verdicts` tables. Write a pytest that inserts and reads back a dummy row in each table.

**Definition of Done:** Schema applied to a real Supabase project; test suite inserts/reads a dummy claim and verdict successfully.

---

### Sprint 2 — Content Intent Classifier
**~3 hrs**

**Goal:** First working agent — classify raw text before anything else runs.

**Prompt for Claude:**
> Implement `app/agents/intent_classifier.py` per `docs/phase1-2-functional-spec.md` §A.3 (Content Intent Classifier row). Use `gpt-4o-mini` with OpenAI's structured output feature to classify input text into the `ContentIntent` model (`FACTUAL_CLAIM`, `OPINION`, `SATIRE_COMEDY`, `FICTIONAL_CREATIVE`, `UNRELATED`) with a confidence score. Write a small hand-crafted test set of 15 examples (3 per category) in `eval/cases/intent_examples.json` and a pytest that runs them through the classifier and reports accuracy.

**Definition of Done:** Classifier correctly labels ≥12/15 hand-crafted examples.

---

### Sprint 3 — Claim Extractor
**~4 hrs**

**Goal:** Turn factual text into a list of atomic, tagged claims.

**Prompt for Claude:**
> Implement `app/agents/claim_extractor.py` per the spec's Claim Extractor row. Input: raw text (already confirmed `FACTUAL_CLAIM` by the intent classifier). Use `gpt-4o` with structured output to return a `list[Claim]`, each tagged with `topic`, `specificity` (`specific`/`general`), and `verifiability_score`. Handle three cases explicitly: a single-claim sentence, a multi-claim paragraph (should split into 2–4 `Claim` objects), and text with zero verifiable claims (should return an empty list, not an error). Add pytest cases for all three.

**Definition of Done:** A hand-written 3-claim paragraph correctly splits into 3 distinct `Claim` objects with sensible tags.

---

### Sprint 4 — Research Agent (single claim, Tavily)
**~5 hrs**

**Goal:** Get one claim all the way to raw search results — the riskiest integration in Phase 1.

**Prompt for Claude:**
> Implement `app/agents/research_agent.py` per the spec's Research Agent row. For a single `Claim`, first build a "Query Decomposer" step (use `gpt-4o-mini` to generate 3–5 distinct search queries covering different angles of the claim), then call the Tavily API (`tavily-python`) for each query and collect raw results (title, url, snippet, published_date if available). Don't rank/score yet — just return the raw combined result list. Get your `TAVILY_API_KEY` from tavily.com's free tier. Write a pytest (mocking the Tavily call) plus one manual test against a real claim.

**Definition of Done:** Given one real claim, the function returns a non-empty list of search results with real URLs.

---

### Sprint 5 — Evidence Ranker + Source Credibility Weighting
**~4 hrs**

**Goal:** Turn raw search results into a scored, structured `EvidencePackage`.

**Prompt for Claude:**
> Implement `app/agents/evidence_ranker.py` per the spec. Input: raw search results (from Sprint 4) + the `Claim`. For each result, look up its domain against the `source_credibility` table (fuzzy-match the domain, e.g., `nytimes.com` matches "Major National Newspapers"; default to a middling weight like 0.4 if no match), classify stance (`for`/`against` the claim — use `gpt-4o-mini` for this classification), and build `EvidenceItem` objects. Sort into `evidence_for`/`evidence_against` lists sorted by `credibility_weight` descending. Assemble and return an `EvidencePackage`. Add a pytest with a handful of mocked search results covering both stances.

**Definition of Done:** Given raw results with a mix of source types, the output `EvidencePackage` correctly sorts high-credibility sources first in each list.

---

### Sprint 6 — Parallelize Research Agents (asyncio)
**~4 hrs**

**Goal:** Run research for multiple claims concurrently — this is the architecture's core value proposition, so it's worth seeing it actually work.

**Prompt for Claude:**
> Refactor `research_agent.py` (and the ranker call) to be `async`. Create `app/pipeline.py` with a function that takes `list[Claim]` and runs the Research Agent + Evidence Ranker for each claim concurrently using `asyncio.gather`, capped at 8 concurrent tasks (use an `asyncio.Semaphore(8)`). If more than 8 claims are passed, batch them in groups of 8, awaiting each batch before starting the next. Add a simple timing test: run 3 claims through both a sequential loop and the new concurrent version, and print/assert that concurrent is meaningfully faster.

**Definition of Done:** A 3-claim submission completes noticeably faster in parallel than run one-at-a-time — literally time it and see the difference.

---

### Sprint 7 — Analyst Agent
**~4 hrs**

**Goal:** Mandatory adversarial synthesis per claim.

**Prompt for Claude:**
> Implement `app/agents/analyst_agent.py` per the spec's Analyst Agent row. Input: `EvidencePackage`. Use `gpt-4o` with a prompt that **requires** both a `for_summary` and `against_summary` in the structured output — if the evidence has nothing contradicting the claim, `against_summary` must literally read "No contradicting evidence found in searched sources," never be blank. Add basic fringe-vs-consensus detection: if ≥90% of *credible* (weight ≥0.75) evidence agrees but low-credibility sources disagree, set a `fringe_vs_consensus_note`. Write pytests including one deliberately one-sided `EvidencePackage` (all evidence supports the claim) to confirm `against_summary` is still populated correctly.

**Definition of Done:** A one-sided test case still produces a non-empty, correctly-worded `against_summary`.

> ⚠️ **Superseded post-Sprint 7:** the fringe-vs-consensus threshold check described above (and `AnalystOutput.fringe_vs_consensus_note`) was removed and replaced with a single capped weighted-evidence-score computed in the Evidence Ranker (`EvidencePackage.confidence_raw`, via `evidence_ranker.compute_evidence_score`) — see `docs/phase1-2-functional-spec.md` §A.2/§A.3 for the current behavior. The prompt text above is kept as the historical record of what Sprint 7 originally built; it is no longer what the code does.

---

### Sprint 8 — Verdict Agent + Citation-Enforcement Validator
**~5 hrs**

**Goal:** The core trust mechanism of the whole product.

**Prompt for Claude:**
> Implement `app/agents/verdict_agent.py` per the spec. Input: `Claim` + `AnalystOutput`. Use `gpt-4o` with a prompt that explicitly instructs: "every factual statement in your rationale must be followed by a citation reference like [SOURCE_1]; you may not make any factual assertion without one." Output must validate against the `Verdict` Pydantic model with the 7-label taxonomy (see `docs/phase1-2-functional-spec.md` §A.2's noted open question — implement exactly the 7 labels as-is, no `DISPUTED`). Add a **two-stage citation validator** (see `docs/phase1-2-functional-spec.md` §A.3a): Stage 1 is the original heuristic, unchanged — scan each sentence for a number, a named entity, or an assertion verb, with no valid `[SOURCE_N]` tag. Stage 2 batches everything Stage 1 flags into one `gpt-4o-mini` structured-output call that classifies each sentence as either a genuinely new, fact-checkable detail (stays flagged) or evaluative/summary language restating an already-cited fact (dropped). The retry/fallback mechanics are unchanged: if anything's still flagged after the two-stage check, re-prompt once with an explicit correction instruction, run the two-stage check again, and only fail if it's still flagged. Write pytest cases that hand-craft evidence to try to trigger at least 4 of the 7 verdict labels, plus these two required regression cases: (a) `"The claim that 'The Eiffel Tower was completed in 1889' is corroborated by clear and reliable evidence."` (with the date fact already cited in an earlier sentence) must NOT be flagged; (b) `"The tower stands 330 meters tall."` (uncited, no obvious trigger verb) must still be flagged.

**Definition of Done:** Verdict output always includes ≥1 valid citation per factual sentence in manual review across several test claims; the re-prompt-on-missing-citation path is demonstrably triggered at least once in testing; both regression cases (a) and (b) pass.

---

### Sprint 9 — Citation Verifier
**~4 hrs**

**Goal:** Independently confirm every citation is real — this is what makes the system trustworthy, not just plausible.

**Prompt for Claude:**
> Implement `app/agents/citation_verifier.py` per the spec. Input: a `Verdict`'s citations list. For each cited URL, fetch the page (use `httpx` with a reasonable timeout and a real User-Agent header), extract readable text (a simple approach like `readability-lxml` or `trafilatura` is fine), and text-search for the specific claim attributed to that citation (use `gpt-4o-mini` to judge "does this page content support the attributed claim?" — a yes/no + short reason). If a citation fails, remove it from `Verdict.citations` and reduce `confidence_score` proportionally (e.g., -15% per removed citation). Write a pytest with one deliberately fake/mismatched citation to confirm it gets caught and removed.

**Definition of Done:** A verdict with one planted bad citation comes out of this step with that citation removed and confidence visibly reduced.

---

### Sprint 10 — Response Formatter + Full Pipeline Wiring (Phase 1 End-to-End!)
**~5 hrs — the big milestone sprint**

**Goal:** Wire every agent from Sprints 2–9 into one working pipeline behind a real API endpoint.

**Prompt for Claude:**
> Implement `app/agents/response_formatter.py` (assembles final `Verdict[]` into a `VerifyResponse`, computing a simple `aggregate_label` when there are multiple claims — e.g., if any claim is `FALSE`, aggregate is at least `PARTIALLY_TRUE`; if any is `MISLEADING`, treat that as equally severe). Then build `app/routes/verify.py` with `POST /verify` (per `docs/phase1-2-functional-spec.md` §A.4) that runs the full pipeline in order: intent classification → claim extraction → parallel research (Sprint 6's function) → analyst → verdict → citation verification → response formatting, persisting the submission/claims/verdicts to Supabase along the way. Wire it into `app/main.py`. Handle the edge cases from spec §A.5 (empty input, zero-claim input, non-factual input short-circuiting).

**Definition of Done:** Paste a real, previously-unseen claim into `POST /verify` (via the FastAPI `/docs` Swagger UI) and get back a full, correctly-labeled, cited `VerifyResponse` in under ~90 seconds. This is genuinely the core product working — worth pausing to enjoy it.

---

### Sprint 11 — Phase 1 Eval Set & Testing
**~4 hrs**

**Goal:** A repeatable way to know if you're breaking things as you keep building.

**Prompt for Claude:**
> Build `eval/run_eval.py`: a script that reads a set of test cases from `eval/cases/verdict_examples.json` (each with input text + an expected verdict label), runs each through `POST /verify` (or calls the pipeline function directly), and reports pass/fail plus overall pass rate. Help me hand-write 20–25 test cases spanning at least 5 of the 7 verdict labels — mix of clearly true claims, clearly false claims, outdated stats, and a misleading-but-technically-accurate example. Include `"The Eiffel Tower was completed in 1889."` (expecting `TRUE`) in the set — this is a known regression case for the two-stage citation validator (§A.3a). Save results to a simple CSV or printed table.

**Definition of Done:** `python eval/run_eval.py` runs the full starter set and reports a pass rate, including a pass on the Eiffel Tower case — this becomes your baseline to grow toward the PRD's eventual 500-case suite.

---

## PHASE 2 — Content Ingestion Pipeline

### Sprint 12 — StructuredContentObject + URL Resolver
**~3 hrs**

**Prompt for Claude:**
> Add the `StructuredContentObject` model to `app/models/schemas.py` per `docs/phase1-2-functional-spec.md` §B.2. Implement `app/ingestion/url_resolver.py`: takes a raw URL string, strips tracking query params, follows redirects to get the canonical URL, and detects the platform (Instagram, or "unknown"). Write pytests with a few messy real-world-shaped Instagram URLs (with `?igsh=` type params) to confirm they resolve cleanly.

**Definition of Done:** A messy Instagram share-link resolves to a clean canonical URL.

---

### Sprint 13 — Upload Endpoint + Caption Path
**~4 hrs**

**Prompt for Claude:**
> Implement `app/ingestion/caption_path.py` per spec §B.3 (Caption Path row): extracts caption text and hashtags/links from provided post metadata. Add `POST /verify/upload` to `app/routes/verify.py` (multipart file upload per spec §B.4) that accepts a video file + optional caption text, and for now (before Sprint 14's downloader exists) just wires the caption straight into a `StructuredContentObject` with `media_type: "video"` and `transcript: null` as a placeholder. Confirm the uploaded file gets saved to a temp location.

**Definition of Done:** Uploading a video file with a caption produces a `StructuredContentObject` with the caption/topics fields correctly populated.

---

### Sprint 14 — Media Downloader (yt-dlp)
**~6 hrs — budget extra time, this is the trickiest sprint**

**Prompt for Claude:**
> Implement `app/ingestion/media_downloader.py` per spec §B.3 and `docs/phase1-2-engineering-requirements.md` §1's `yt-dlp` decision. Given a canonical Instagram URL (from Sprint 12's resolver), use `yt-dlp` to download the video to a temp file. Handle failure gracefully: if `yt-dlp` fails (private post, deleted, rate-limited), catch the error and return a clear "could not download this content" result rather than raising — per spec §B.5, this should trigger a caption-only fallback path if any caption text is available, otherwise a clean user-facing error. Add `POST /verify/url` to the routes. Test against a real public Reel URL and against a deliberately broken/private URL to confirm both paths.

**Definition of Done:** A real public Reel URL downloads successfully; a broken URL fails gracefully with a clear message, not a crash.

---

### Sprint 15 — ffmpeg + Whisper Transcription
**~4 hrs**

**Prompt for Claude:**
> Implement the audio portion of `app/ingestion/video_path.py` per spec §B.3 (Video Path row). Use `ffmpeg` (via `subprocess` or `ffmpeg-python`) to extract an audio track from the downloaded video. Send it to OpenAI's Whisper API for transcription. Whisper doesn't return a confidence score directly — approximate "low confidence" by checking for the `no_speech_prob` in verbose JSON output (if available) or by falling back to a simple heuristic (e.g., transcript length near zero relative to video duration → flag `low_confidence_transcript = True`). Test against a video with clear speech and a music-only video with no speech.

**Definition of Done:** A speech-containing video produces a real transcript; a music-only video correctly sets `low_confidence_transcript: true` rather than hallucinating text.

---

### Sprint 16 — Frame Sampler + GPT-4o Vision
**~5 hrs**

**Prompt for Claude:**
> Add frame sampling to `app/ingestion/video_path.py`: use `ffmpeg` to extract 1 frame per second from the video into temp image files. Send frames (batch a reasonable subset if the video is long, e.g., every 3rd sampled frame to control cost) to GPT-4o's vision capability with a prompt asking it to describe visual content **and** transcribe any on-screen text (captions, overlaid stats, etc.). Combine results into the `visual_context` field. Test against a video that has on-screen text overlays to confirm the OCR portion actually captures it.

**Definition of Done:** A video with visible on-screen text (e.g., a stat overlay) has that text correctly captured in `visual_context`.

---

### Sprint 17 — Media Router + Object Assembly
**~4 hrs**

**Prompt for Claude:**
> Implement `app/ingestion/media_router.py` per spec §B.3: given a downloaded file (or caption-only input), determine `media_type` (`video`/`image`/`text_post`) and dispatch to the Video Path (Sprints 15–16) or Caption Path (Sprint 13) accordingly, assembling the final `StructuredContentObject` regardless of which path ran. Write a schema-consistency pytest that runs all three input types (a real video, a static image with text, a caption-only text post) through the router and asserts the output object has the same field structure in all three cases (even if some fields are `null`).

**Definition of Done:** All three media types produce a `StructuredContentObject` with identical shape — this is the hard interface contract the spec calls out, worth actually testing, not just assuming.

---

### Sprint 18 — Full Phase 2 Integration + Supabase Storage
**~5 hrs — second big milestone sprint**

**Prompt for Claude:**
> Wire everything from Sprints 12–17 into `POST /verify/url` and `POST /verify/upload` so that ingestion output (the `StructuredContentObject`) feeds directly into the Phase 1 pipeline from Sprint 10 (treat the combined `transcript + visual_context + caption` as the text input to the intent classifier/claim extractor — concatenate with clear section labels). Add Supabase Storage integration: uploaded/downloaded video files get stored temporarily and auto-deleted after 24 hours (implement as a scheduled cleanup check, or a stored `expires_at` timestamp checked on each request for now — a full cron job isn't required yet); transcripts get stored with 90-day retention in the `submissions` table or a linked table.

**Definition of Done:** Paste a real public Instagram Reel URL into `POST /verify/url` and get back a complete, cited verdict — the full Phase 1 + Phase 2 pipeline working end-to-end on real content. This is the actual product working for the first time.

---

## After Sprint 18

You'll have a working core engine (Phase 1) plus real Reel ingestion (Phase 2) — the hardest, highest-uncertainty parts of the whole system. Phase 3 (Instagram DM channel, Meta App Review, production queueing) and Phase 4 (dedup, circuit breakers, full observability) are meaningfully lower-risk from here, since they're mostly wiring a known-working core into new transports rather than solving new problems. Worth a genuine pause here before continuing.
