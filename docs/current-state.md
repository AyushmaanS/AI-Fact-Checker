# Current State
**Last updated:** 2026-09-20 · after Sprint 13 + 4 Phase-1 retrofits · commit `49bf3d8`

Living snapshot of what's actually true in the code right now. The other docs in
this folder (`prd-v1-draft.md`, `phase1-2-functional-spec.md`,
`phase1-2-engineering-requirements.md`, `phase1-2-sprint-plan.md`) describe intent
and history; this one describes reality, and should be updated whenever reality
and those diverge.

---

## 1. What's implemented

**Phase 1 (Sprints 0–11): done.** Phase 2 (Sprints 12–18, video/image ingestion):
**started** — Sprints 12–13 done, 14–18 not started.

| Sprint | What it built |
|---|---|
| 0 | FastAPI scaffold, `GET /health`, `.env` config loading |
| 1 | Pydantic schemas, Postgres DDL, Supabase client, seeded `source_credibility` (8 tiers, ~30 domains) |
| 2 | Content Intent Classifier (`gpt-4o-mini`) |
| 3 | Claim Extractor (`gpt-4o`) |
| 4 | Research Agent (Tavily + query decomposer) |
| 5 | Evidence Ranker + credibility weighting (rebuilt once since — see §9) |
| 6 | asyncio concurrency (research), later extended to the full per-claim chain |
| 7 | Analyst Agent (rebuilt twice since — see §9) |
| 8 | Verdict Agent + citation enforcement (rebuilt three times since — see §9) |
| 9 | Citation Verifier (updated once since — see §9) |
| 10 | Response Formatter + full pipeline wiring (`POST /verify`) (updated once since — see §9) |
| 11 | Eval set (22 hand-written cases) + `eval/run_eval.py` |
| 12 | `StructuredContentObject` model + `app/ingestion/url_resolver.py` (Phase 2 start) |
| 13 | `app/ingestion/caption_path.py` + `POST /verify/upload` (ingestion only — see §9) |

Plus 4 retrofits not tied to a sprint number, each shipped after the sprint that
introduced the thing it replaced, each verified live before landing (details in §9):
1. Fringe-vs-consensus threshold → capped weighted-evidence-score
2. Free-text rationale + heuristic-only validator → two-stage validator (heuristic + LLM semantic check)
3. Two-stage validator → structured self-declared `RationaleSegment`s
4. Structured segments → **evidence lines + one audited summary line** (current — see §2/§3)

**Sprint 12 is a standalone utility, not wired into a route yet** —
`app/ingestion/url_resolver.py`'s `resolve_url()` has no caller in the codebase.
Sprint 13 (`POST /verify/upload`) doesn't call it either per its own prompt (that
sprint is about the caption path + upload endpoint, not URL handling); the
resolver's first real caller is likely whichever future sprint adds
`POST /verify/url`.

**`POST /verify/upload` ingests but does not fact-check yet, by design (per
Sprint 13's own prompt).** It saves the upload to a temp file, builds a
`StructuredContentObject` from the caption (via `caption_path.extract_caption_content`),
persists a `submissions` row, and returns a `VerifyResponse` with `claims: []`,
`verdicts: []`, and an explanatory `message` — it does **not** run the
`StructuredContentObject` through the Phase 1 pipeline yet. `transcript` is always
`None` (no downloader/transcription until Sprint 14+). See §9 for how this
compares to functional-spec §B.4's eventual full contract.

---

## 2. Architecture / pipeline

```
POST /verify {text}
  → classify_intent (gpt-4o-mini)          non-FACTUAL_CLAIM short-circuits, canned message
  → extract_claims (gpt-4o)                 zero claims short-circuits, canned message
  → pipeline.process_claims(claims)         up to 8 claims concurrent, per claim:
      1. research_claim        Tavily, 3-5 queries (gpt-4o-mini decomposer), run concurrently
      2. rank_evidence          gpt-4o-mini: stance + one-line paraphrase per item, same call;
                                 evidence_id generated in code; credibility-weighted sort +
                                 compute_evidence_score (EVIDENCE_CAP=5, unchanged)
      3. analyze_evidence       selected_for_ids/selected_against_ids: PLAIN CODE, top 3 per
                                 side by credibility_weight (SELECTED_EVIDENCE_CAP=3), no LLM
                                 call; gpt-4o call sets outdated_flag only, sees full package
      4. produce_verdict        code: resolves selected ids -> EvidenceLines (paraphrase +
                                 citation + stance + weight); ONE gpt-4o call produces ONLY
                                 label + summary_line + confidence_score - no citation field
                                 exists for it to get wrong; audit_summary_line checks
                                 summary_line for smuggled-in new facts; 1 retry; if still
                                 failing, falls back to UNVERIFIABLE IN PLACE, keeping the
                                 real evidence_lines/citations - never raises
      5. verify_citations       gpt-4o-mini + httpx fetch, per EvidenceLine's paraphrase;
                                 failed fetch = inconclusive (no penalty);
                                 failed semantic match = whole EvidenceLine removed (not just
                                 its citation string), citations recomputed from survivors,
                                 confidence *= 0.85 per removed line;
                                 summary_line is never fetched/verified - exempt by design
  → format_response                         aggregate_label (worst-case-wins), assembles VerifyResponse
  → DB writes: submissions, claims, verdicts (rationale column = response_formatter.format_verdict_text:
    each evidence line's paraphrase+citation, one per line, then summary_line last)
```

Agent files: `intent_classifier.py`, `claim_extractor.py`, `research_agent.py`,
`evidence_ranker.py`, `analyst_agent.py`, `verdict_agent.py`, `citation_verifier.py`,
`response_formatter.py`. Orchestration: `pipeline.py` (concurrency — untouched by
retrofit 4 except removing a now-dead try/except, see §9), `routes/verify.py`
(HTTP + DB persistence). Shared: `llm_client.py` (FastRouter client + model IDs,
60s timeout), `db/client.py` (Supabase).

---

## 3. Important design decisions

- **FastRouter, not OpenAI direct.** `gpt-4o`/`gpt-4o-mini` via `api.fastrouter.ai`,
  dated model IDs (`openai/gpt-4o-2024-11-20`, `openai/gpt-4o-mini-2024-07-18` —
  the `2024-05-13` snapshot doesn't support Structured Outputs, found via a live 400).
- **Tavily-only search**, not the PRD's 5 dedicated source integrations (explicit
  solo-builder scoping decision from the engineering doc).
- **Two different "top N" caps, at two different layers, for two different jobs —
  don't confuse them:** `EVIDENCE_CAP = 5` (`evidence_ranker.py`) only affects
  `confidence_raw` scoring. `SELECTED_EVIDENCE_CAP = 3` (`analyst_agent.py`) governs
  how many `EvidenceItem`s per side actually become `EvidenceLine`s shown to the
  Verdict Agent. A claim can have its evidence-balance score computed from 5+5
  items while the Verdict Agent itself only ever sees 3+3.
- **`confidence_raw`** = credibility-weighted "for" share of the capped top sources
  (`evidence_ranker.compute_evidence_score`), not a simple count. Untouched by
  retrofit 4.
- **Current design (retrofit 4): the citation-attachment failure mode is eliminated
  structurally, not detected after the fact.** Every source is paraphrased exactly
  once, at rank time, while it's still a fixed object (`EvidenceItem.paraphrase`,
  Evidence Ranker). Selection of which evidence reaches the Verdict Agent is plain
  code (Analyst Agent), not an LLM judgment call. The Verdict Agent's one LLM call
  (`VerdictAgentOutput`) has no field to put a URL in — it can only ever write
  `label` + `summary_line` + `confidence_score` — so it is structurally unable to
  mismatch a citation, unlike every earlier design (`[SOURCE_N]` tags, then
  self-declared rationale segments), which all had to *detect* a bad citation after
  the model had already written one.
- **`summary_line` audit is detail-level, not whole-sentence** (`audit_summary_line`
  / `_extract_factual_details`, carried over unchanged from the segment design's
  hard-won live-testing fixes): extracts numbers/dates/proper nouns from
  `summary_line` and checks each against the evidence lines' combined paraphrase
  text, so a closing sentence that legitimately paraphrases an already-shown fact
  ("completed on March 31, 1889" vs. "concluded in March 1889") doesn't get wrongly
  flagged.
- **Audit failure now degrades in place instead of raising.** If `summary_line`
  still fails the audit after one retry, `produce_verdict` returns `UNVERIFIABLE`
  with the real `evidence_lines`/`citations` kept intact and only `summary_line`
  swapped for a canned explanation. `produce_verdict` never raises at all now — the
  prior `VerdictCitationError` class is gone, and so is the try/except in
  `pipeline._process_one_claim` that used to catch it (nothing left to catch there).
- **Citation Verifier removes the whole `EvidenceLine`, not just its citation
  string, on a failed check** — a real behavior change from every earlier design,
  caught by live testing (see §9): if two evidence lines share one citation URL and
  only one is judged unsupported, the surviving line's citation correctly stays in
  `citations`. The old per-URL removal was coarser — one bad line used to be able to
  wipe out a citation another, legitimate line also depended on.
- **Citation Verifier: fetch failure ≠ verification failure.** Wikipedia and Reuters
  both block automated fetches outright (403/401) regardless of User-Agent. An
  unfetchable `EvidenceLine` is left untouched, not penalized — "couldn't check"
  isn't "checked and wrong."
- **Confidence penalty:** 15% multiplicative per removed `EvidenceLine` (`0.85 ** N`).
- **`aggregate_label`:** worst-case-wins, with FALSE/MISLEADING treated as equally
  severe. A single FALSE/MISLEADING claim mixed among otherwise-fine ones caps the
  aggregate at PARTIALLY_TRUE (not full FALSE); only when *every* claim is
  FALSE/MISLEADING does the aggregate report the worse of those two directly.
- **`VerifyResponse.message`** (not in the original spec): carries the canned
  explanation for non-factual-intent and zero-claim short-circuits, since
  `aggregate_label` is meant to hold a verdict label, not free text.
- **LLM client timeout: 60s**, not the OpenAI SDK's 600s default (found live, prior
  retrofit).
- **URL Resolver (Sprint 12) skips the network entirely for a URL whose domain is
  already a recognized platform** (currently just `instagram.com`/`instagr.am`) —
  only strips tracking params. Confirmed live: an unauthenticated HEAD to a
  made-up Instagram reel URL 200s with the same URL back (no block, no redirect,
  since the real content loads client-side) — so a live fetch there wouldn't add
  information anyway, unlike a genuine shortlink domain, which does get a
  best-effort redirect-follow (`_follow_redirects`, graceful fallback to the
  original URL on any failure — same "can't check, not a failure" stance as
  Citation Verifier).

---

## 4. Database schema (Supabase Postgres) — unchanged since Sprint 1

```sql
submissions(id uuid pk, raw_input text, input_type text, created_at)
claims(id uuid pk, submission_id fk, text, topic, specificity, verifiability_score)
verdicts(id uuid pk, claim_id fk, label, rationale text, confidence_score, citations jsonb, created_at)
source_credibility(domain_pattern text pk, category text, weight float)  -- seeded, ~30 rows
```

`verdicts.rationale` is still a plain `text` column — the DDL never changed. What
changed (again) is what Python writes into it: `response_formatter.format_verdict_text()`
now renders each `EvidenceLine` (its paraphrase, with its citation) one per line,
followed by `summary_line` as the closing line, before the DB write
(`routes/verify.py`). The in-memory `Verdict` object no longer has any free-text
rationale field at all — just `evidence_lines` (structured) and `summary_line` (one
sentence, audited).

No vector DB, no Redis, no job queue — all explicitly deferred to Phase 4+.

---

## 5. API endpoints

| Endpoint | Status |
|---|---|
| `GET /health` | ✅ implemented |
| `POST /verify` | ✅ implemented (the only real endpoint) |
| `GET /verdicts/{submission_id}` | ❌ **not implemented** — speced in functional-spec §A.4, deliberately out of Sprint 10's scope |
| `POST /verify/upload` | ✅ implemented, ingestion-only (Sprint 13) — accepts a video + optional caption, saves it, returns an explanatory message instead of a real verdict; see §1 |
| `POST /verify/url` | Phase 2, not started |

---

## 6. Environment variables (`.env`)

| Var | Used by | Status |
|---|---|---|
| `FASTROUTER_API_KEY` | all LLM calls | active |
| `TAVILY_API_KEY` | research_agent | active (on a third key as of 2026-09-20 — both the original and the one pre-authorized backup hit `ForbiddenError: usage limit` during this session; project owner supplied a new key to unblock the eval rerun in §8) |
| `SUPABASE_URL` / `SUPABASE_KEY` | db/client.py | active |
| `OPENAI_API_KEY` | — | **present in `.env.example` but unused** — no code path calls OpenAI directly; reserved for Whisper transcription once Phase 2 starts, may or may not still be needed depending on whether FastRouter proxies audio endpoints (never checked) |

---

## 7. Known bugs / tech debt

- **No per-claim error isolation in `pipeline.process_claims`, still true.**
  `_process_one_claim` no longer wraps anything in a try/except at all (`produce_verdict`
  itself no longer raises, so there was nothing left there to catch) — but that
  never covered this gap anyway. Any exception from `research_claim`,
  `rank_evidence`, `analyze_evidence`, or `verify_citations` still propagates out of
  `asyncio.gather` and kills the *entire* concurrent batch, not just the one claim.
  Confirmed live during this session: a Tavily `ForbiddenError` (quota exhaustion,
  since resolved — see §6) in the first batch took down all 22 eval cases at once,
  with zero of the other 21 claims' results recoverable. `research_agent.py` is
  explicitly off-limits to modify per current instructions, so this is unresolved.
- **Residual audit false-positives (same underlying limitation, smaller blast
  radius now).** `audit_summary_line` matches extracted details (numbers/proper
  nouns) against evidence-line paraphrase text as exact substrings — a `summary_line`
  using different notation for the same fact ("100°C" vs "100 degrees Celsius") or a
  rounded figure (exact "13,171 miles" vs. rounded "13,000") can still get wrongly
  flagged. Inherent cost of deterministic string matching vs. an LLM judgment call.
  Two things are better than before, though: only one sentence (`summary_line`) is
  ever audited per verdict now, versus potentially several `connective_reasoning`
  segments before, so there are fewer chances to trip it — and when it does fire,
  the fallback no longer discards the real evidence/citations, only the closing
  sentence. Not directly implicated in any of the post-retrofit-4 eval failures
  (§8) — all 5 were clean label disagreements, not audit-fallback UNVERIFIABLEs.
- **MISLEADING vs FALSE calibration — confirmed still present post-retrofit-4.**
  The model consistently prefers a clean FALSE over MISLEADING for absolute/
  totalizing claims ("entirely," "completely"). Re-confirmed by the same two eval
  cases as before the retrofit (Ming Dynasty Great Wall, "bats are completely
  blind" — see §8): both failed the exact same way under the new design. Likely an
  eval ground-truth-strictness question, not a code bug — the label itself is a
  judgment call, and the pipeline mechanism produced a clean, well-formed verdict
  in both cases.
- **OUTDATED vs FALSE ambiguity:** genuine taxonomy-boundary overlap (the Pluto
  eval case, which passed this run) — not a bug, inherent taxonomy fuzziness.
- **New, single-instance observation (not yet a confirmed pattern):** a claim
  about an inherently unverifiable private document ("a private letter... never
  publicly released...") got FALSE instead of the expected UNVERIFIABLE — see §8.
  Plausibly the model treating strong indirect/circumstantial evidence (e.g. Van
  Gogh's well-documented extensive use of yellow) as sufficient to contradict the
  claim, rather than recognizing that no public source could confirm or deny a
  private, unpublished document either way. One data point; worth watching on
  future eval runs before treating as a real pattern.
- **`GET /verdicts/{submission_id}`** not implemented (see §5).

---

## 8. Test / eval status

**79 tests collected** across 14 test files (`test_main`, `test_schemas`,
`test_db`, `test_intent_classifier`, `test_claim_extractor`, `test_research_agent`,
`test_evidence_ranker`, `test_analyst_agent`, `test_verdict_agent`,
`test_citation_verifier`, `test_verify_route`, `test_pipeline`, `test_url_resolver`,
`test_caption_path`). The 10 `test_url_resolver` tests are Sprint 12's, and 5 more
in `test_caption_path` plus 5 upload-endpoint tests added to `test_verify_route`
are Sprint 13's — all 20 deterministic, no network, no live-key gating. **All
pass**, including every live test
(needs `FASTROUTER_API_KEY` / `TAVILY_API_KEY` / Supabase creds) — confirmed via
two full consecutive runs after retrofit 4, the second one clean, plus the
non-live subset (74 tests) confirmed clean again after Sprints 12 and 13, most
recently 74 passed / 5 deselected. One live test's own expectation had to be
fixed along the way, during retrofit 4: the old
"per-URL" citation removal test asserted a shared citation gets fully wiped when
any one of its lines fails; that's no longer correct under the new fine-grained
per-`EvidenceLine` removal (§3), so the assertion was corrected and a deterministic
regression test for the same "shared URL, one valid line" case was added
(`test_shared_citation_url_survives_if_one_of_its_lines_is_valid`) — not a code bug,
a stale test expectation caught by live testing doing exactly its job.

**`eval/run_eval.py` post-retrofit-4: 17/22 (77%), 63.7s total** — above the prior
design's 64–73% range, so no regression (this single run is also each design's
best recorded pass rate so far; not enough runs post-retrofit to know if 77% is
the new typical or a good roll — LLM output variance applies here same as before).
None of the 5 failures show the audit-fallback signature (UNVERIFIABLE + the canned
`AUDIT_FALLBACK_SUMMARY` text) — every case got a clean, well-formed label from the
pipeline; all 5 mismatches are the model's label choice disagreeing with the eval's
ground truth, not a pipeline/mechanism defect. Failures:

| Claim | Expected | Actual | Note |
|---|---|---|---|
| "Water boils at 100 degrees Celsius at sea level atmospheric pressure." | TRUE | MISLEADING | New. Claim already correctly qualifies itself; model likely over-weighted boiling-point-varies-with-altitude context despite the qualifier. |
| "The Statue of Liberty was a gift from the French government to the United States government to celebrate American independence." | PARTIALLY_TRUE | TRUE | New. Eval expects the French-public-subscription-vs-government nuance to be caught; evidence gathered apparently didn't surface it strongly enough. |
| "The Great Wall of China was built entirely during the Ming Dynasty." | MISLEADING | FALSE | Same case, same failure, as the pre-retrofit design (§7 calibration note). |
| "Bats are completely blind." | MISLEADING | FALSE | Same case, same failure, as the pre-retrofit design (§7 calibration note). |
| "A private letter written by Vincent van Gogh in 1881, never publicly released, mentions his fear of the color yellow." | UNVERIFIABLE | FALSE | New. See §7's single-instance observation. |

This satisfies the user's explicit retrofit-4 request ("re-run the eval set in
full... and confirm no regressions") — retrofit 4 is now fully complete.

---

## 9. Deviations from the sprint prompts / functional spec (cumulative)

- **All LLM calls routed through FastRouter**, not `api.openai.com` directly —
  explicit user decision, post-Sprint 2.
- **Sprint 6:** also parallelized the 3–5 queries *within* one claim's research,
  not just claim-vs-claim (spec only asked for the latter).
- **Sprint 7:** Analyst Agent takes `Claim` + `EvidencePackage`, not just
  `EvidencePackage` as the spec's table says — needed the claim text to do its job.
- **Post-Sprint 7 retrofit 1:** `AnalystOutput.fringe_vs_consensus_note` and its
  detection logic removed entirely; evidence-balance scoring moved to Evidence
  Ranker's `confidence_raw`.
- **Sprint 8:** Verdict Agent takes `Claim` + `AnalystOutput` + `EvidencePackage`,
  not just `Claim` + `AnalystOutput` — needed real source URLs to cite (retrofit 4:
  needed to resolve `AnalystOutput`'s selected ids back into `EvidenceItem`s), which
  `AnalystOutput` alone doesn't carry either way.
- **Sprint 8 rebuilt three times post-shipping total** (see §1): heuristic +
  hardcoded exempt-phrase list (retrofit 2) → heuristic + `gpt-4o-mini` semantic
  classifier → structured self-declared rationale segments (retrofit 3) →
  evidence lines + one audited `summary_line`, current (retrofit 4). Each rebuild
  targeted a specific, live-testing-demonstrated failure class in the previous
  version; retrofit 4 went further and made the failure mode structurally
  unreachable instead of detecting it (see §3).
- **Sprint 9:** Citation Verifier's "fetch failure is inconclusive, not a failure"
  design isn't in the spec — discovered via live testing that Wikipedia/Reuters
  block automated fetches outright. Retrofit 4 additionally changed removal
  granularity from per-URL to per-`EvidenceLine` (see §3), also found via live
  testing (a stale test assertion, not a production bug — see §8).
- **Sprint 10:** `VerifyResponse` gained a `message` field not in the original
  spec; `GET /verdicts/{submission_id}` was never built; `intent_classifier.py`/
  `claim_extractor.py` were converted from sync to async ahead of Sprint 6's
  conversion. Retrofit 4 additionally simplified `pipeline._process_one_claim`
  by removing its try/except around the Verdict Agent call, now dead code since
  `produce_verdict` no longer raises.
- **Retrofit 4's own instructions referenced `docs/phase1-2-functional-spec.md`
  §A.3b as an existing "reference implementation" to build from — it didn't exist
  yet.** Same situation as retrofit 2, which referenced a not-yet-existing §A.3b and
  ended up creating §A.3a instead (no §A.3a existed at that point either). This
  time §A.3a already existed (from retrofit 2), so §A.3b was free — created fresh
  as part of this retrofit, marking §A.3a superseded rather than deleting it.
- **Retrofit 4's instructions described a "small wiring fix" needed in
  `routes/verify.py`** to pass `EvidencePackage` through to the Verdict Agent step.
  In the actual codebase, `routes/verify.py` never called the Verdict Agent
  directly — `pipeline.py` does, and its existing call
  (`produce_verdict(claim, analyst, evidence)`) already threaded `EvidencePackage`
  through, before this retrofit. No signature change was needed anywhere. The one
  real, necessary change to `routes/verify.py` was renaming the import/call from
  `join_rationale_segments` to `format_verdict_text` to match the Response
  Formatter's new API.
- **Today (2026-09-20):** eval rerun briefly blocked on Tavily quota exhaustion on
  both the original and pre-authorized backup keys; project owner supplied a third
  key to unblock it (see §6). Rerun then completed at 17/22 (77%), no regression —
  see §8.
- **Sprint 12:** `python-multipart` wasn't in `requirements.txt` despite being
  required for any FastAPI `File`/`Form`/`UploadFile` endpoint — not needed until
  Sprint 13 actually added one, discovered and fixed then (installed + pinned).
- **Sprint 13:** `POST /verify/upload` does not run the ingested content through
  the Phase 1 pipeline, unlike functional-spec §B.4's eventual full contract
  ("runs ingestion, then the full Phase 1 pipeline" → real `VerifyResponse` with
  claims/verdicts). This matches Sprint 13's own prompt, which explicitly scopes
  this sprint to "for now... just wires the caption straight into a
  StructuredContentObject... as a placeholder" — the DoD only checks that object's
  fields, not a real verdict. The response is still a `VerifyResponse` (keeping
  §B.4's response *type* contract), just with empty `claims`/`verdicts` and an
  explanatory `message`, reusing the same mechanism Sprint 10 built for the
  non-factual-intent/zero-claim short circuits. No sprint has yet specified when
  `StructuredContentObject` actually gets handed to the Phase 1 engine (spec §B.1
  step 6 describes this as the eventual end state, not tied to a sprint number).
