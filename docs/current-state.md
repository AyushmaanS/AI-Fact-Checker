# Current State
**Last updated:** 2026-09-19 · after Sprint 11 + 3 post-hoc retrofits · commit `1c9e110`

Living snapshot of what's actually true in the code right now. The other docs in
this folder (`prd-v1-draft.md`, `phase1-2-functional-spec.md`,
`phase1-2-engineering-requirements.md`, `phase1-2-sprint-plan.md`) describe intent
and history; this one describes reality, and should be updated whenever reality
and those diverge.

---

## 1. What's implemented

**Phase 1 (Sprints 0–11): done.** Phase 2 (Sprints 12–18, video/image ingestion):
**not started** — `app/ingestion/` is an empty package stub from Sprint 0 scaffolding
only.

| Sprint | What it built |
|---|---|
| 0 | FastAPI scaffold, `GET /health`, `.env` config loading |
| 1 | Pydantic schemas, Postgres DDL, Supabase client, seeded `source_credibility` (8 tiers, ~30 domains) |
| 2 | Content Intent Classifier (`gpt-4o-mini`) |
| 3 | Claim Extractor (`gpt-4o`) |
| 4 | Research Agent (Tavily + query decomposer) |
| 5 | Evidence Ranker + credibility weighting |
| 6 | asyncio concurrency (research), later extended to the full per-claim chain |
| 7 | Analyst Agent (for/against synthesis) |
| 8 | Verdict Agent + citation enforcement (rebuilt twice since — see §9) |
| 9 | Citation Verifier |
| 10 | Response Formatter + full pipeline wiring (`POST /verify`) |
| 11 | Eval set (22 hand-written cases) + `eval/run_eval.py` |

Plus 3 retrofits not tied to a sprint number, all shipped after the sprint that
introduced the thing they replaced (details in §9):
1. Fringe-vs-consensus threshold → capped weighted-evidence-score
2. Free-text rationale + heuristic-only validator → two-stage validator (heuristic + LLM semantic check)
3. Two-stage validator → structured self-declared `RationaleSegment`s (current)

---

## 2. Architecture / pipeline

```
POST /verify {text}
  → classify_intent (gpt-4o-mini)          non-FACTUAL_CLAIM short-circuits, canned message
  → extract_claims (gpt-4o)                 zero claims short-circuits, canned message
  → pipeline.process_claims(claims)         up to 8 claims concurrent, per claim:
      1. research_claim        Tavily, 3-5 queries (gpt-4o-mini decomposer), run concurrently
      2. rank_evidence          credibility-weighted sort + compute_evidence_score
      3. analyze_evidence       gpt-4o: for_summary/against_summary (never blank) + outdated_flag
      4. produce_verdict        gpt-4o: writes rationale_segments directly (see §3);
                                 Pydantic validates citations; audit_connective_segments
                                 checks for smuggled facts; 1 retry; else VerdictCitationError
      5. verify_citations       gpt-4o-mini + httpx fetch, per sourced_fact segment;
                                 failed fetch = inconclusive (no penalty);
                                 failed semantic match = citation removed, confidence *= 0.85
      ↳ VerdictCitationError caught here → graceful UNVERIFIABLE fallback, never a 500
  → format_response                         aggregate_label (worst-case-wins), assembles VerifyResponse
  → DB writes: submissions, claims, verdicts (rationale column = joined segment text)
```

Agent files: `intent_classifier.py`, `claim_extractor.py`, `research_agent.py`,
`evidence_ranker.py`, `analyst_agent.py`, `verdict_agent.py`, `citation_verifier.py`,
`response_formatter.py`. Orchestration: `pipeline.py` (concurrency), `routes/verify.py`
(HTTP + DB persistence). Shared: `llm_client.py` (FastRouter client + model IDs),
`db/client.py` (Supabase).

---

## 3. Important design decisions

- **FastRouter, not OpenAI direct.** `gpt-4o`/`gpt-4o-mini` via `api.fastrouter.ai`,
  dated model IDs (`openai/gpt-4o-2024-11-20`, `openai/gpt-4o-mini-2024-07-18` —
  the `2024-05-13` snapshot doesn't support Structured Outputs, found via a live 400).
- **Tavily-only search**, not the PRD's 5 dedicated source integrations (explicit
  solo-builder scoping decision from the engineering doc).
- **`EVIDENCE_CAP = 5`**: caps evidence per side for both `confidence_raw` scoring
  and the Verdict Agent's candidate source list — prevents a flood of low-credibility
  sources from outvoting a few high-credibility ones.
- **`confidence_raw`** = credibility-weighted "for" share of the capped top sources
  (`evidence_ranker.compute_evidence_score`), not a simple count.
- **Rationale is structured, not free text** (current design, retrofit #3): the
  Verdict Agent outputs `rationale_segments`, each self-labeled `sourced_fact`
  (citation required, checked by Pydantic) or `connective_reasoning` (no citation,
  audited by a narrow heuristic for smuggled-in new facts). No more `[SOURCE_N]`
  tag parsing anywhere in the codebase.
- **Citation Verifier: fetch failure ≠ verification failure.** Wikipedia and Reuters
  both block automated fetches outright (403/401) regardless of User-Agent. An
  unfetchable citation is left untouched, not penalized — "couldn't check" isn't
  "checked and wrong."
- **Confidence penalty:** 15% multiplicative per removed citation (`0.85 ** N`).
- **`aggregate_label`:** worst-case-wins, with FALSE/MISLEADING treated as equally
  severe. A single FALSE/MISLEADING claim mixed among otherwise-fine ones caps the
  aggregate at PARTIALLY_TRUE (not full FALSE); only when *every* claim is
  FALSE/MISLEADING does the aggregate report the worse of those two directly.
- **`VerifyResponse.message`** (not in the original spec): carries the canned
  explanation for non-factual-intent and zero-claim short-circuits, since
  `aggregate_label` is meant to hold a verdict label, not free text.
- **LLM client timeout: 60s**, not the OpenAI SDK's 600s default (found live —
  see §7).

---

## 4. Database schema (Supabase Postgres) — unchanged since Sprint 1

```sql
submissions(id uuid pk, raw_input text, input_type text, created_at)
claims(id uuid pk, submission_id fk, text, topic, specificity, verifiability_score)
verdicts(id uuid pk, claim_id fk, label, rationale text, confidence_score, citations jsonb, created_at)
source_credibility(domain_pattern text pk, category text, weight float)  -- seeded, ~30 rows
```

`verdicts.rationale` is still a plain `text` column — the DDL never changed. What
changed is what Python writes into it: `response_formatter.join_rationale_segments()`
joins the structured segments into one paragraph before the DB write
(`routes/verify.py`). The in-memory `Verdict` object itself no longer has a
`.rationale` string field at all.

No vector DB, no Redis, no job queue — all explicitly deferred to Phase 4+.

---

## 5. API endpoints

| Endpoint | Status |
|---|---|
| `GET /health` | ✅ implemented |
| `POST /verify` | ✅ implemented (the only real endpoint) |
| `GET /verdicts/{submission_id}` | ❌ **not implemented** — speced in functional-spec §A.4, deliberately out of Sprint 10's scope |
| `POST /verify/upload`, `POST /verify/url` | Phase 2, not started |

---

## 6. Environment variables (`.env`)

| Var | Used by | Status |
|---|---|---|
| `FASTROUTER_API_KEY` | all LLM calls | active |
| `TAVILY_API_KEY` | research_agent | active (swapped once today after the original key hit its usage limit) |
| `SUPABASE_URL` / `SUPABASE_KEY` | db/client.py | active |
| `OPENAI_API_KEY` | — | **present in `.env.example` but unused** — no code path calls OpenAI directly; reserved for Whisper transcription once Phase 2 starts, may or may not still be needed depending on whether FastRouter proxies audio endpoints (never checked) |

---

## 7. Known bugs / tech debt

- **Residual audit false-positives (low frequency, understood, not chased further):**
  `audit_connective_segments` matches specific extracted details (numbers/proper
  nouns) against sourced-fact text as exact strings. A connective sentence that
  paraphrases a fact using different notation ("100°C" vs "100 degrees Celsius")
  or a rounded figure (exact "13,171 miles" vs. rounded "13,000") can still get
  wrongly flagged, trigger the one retry, and occasionally still fail →
  `VerdictCitationError` → graceful UNVERIFIABLE fallback that masks an otherwise-
  correct verdict. Inherent cost of deterministic string matching vs. an LLM
  judgment call, which this design deliberately moved away from.
- **No per-claim error isolation in `pipeline.process_claims`.** Only
  `VerdictCitationError` is caught per-claim; any other exception (e.g. a raw
  `tavily.errors.TimeoutError` from `research_agent.py`) propagates out of
  `asyncio.gather` and kills the *entire* batch, not just the one claim. Observed
  live today during an eval run. `research_agent.py` is explicitly off-limits to
  modify per current instructions, so this is unresolved.
- **MISLEADING vs FALSE calibration:** the model consistently prefers FALSE over
  MISLEADING for absolute/totalizing claims ("entirely," "completely" — the Ming
  Dynasty and "bats are blind" eval cases). Likely the eval's ground-truth labels
  are too strict, not a code bug — unresolved, no action taken.
- **OUTDATED vs FALSE ambiguity:** genuine taxonomy-boundary overlap (the Pluto
  eval case) — both labels are defensible under the spec's own definitions.
  Not a bug, just an inherent taxonomy fuzziness, unresolved.
- **`test_verify_route.py`'s live end-to-end test** intermittently hit the SDK's
  old 600s default timeout before today's `llm_client.py` fix (confirmed: two
  runs at ~618s/625s). Now bounded to a ~20-180s worst case, not fully eliminated
  — FastRouter could still stall, just no longer for up to 10 minutes.
- **`GET /verdicts/{submission_id}`** not implemented (see §5).

---

## 8. Test / eval status

**57 tests collected** across 11 test files (`test_main`, `test_schemas`,
`test_db`, `test_intent_classifier`, `test_claim_extractor`, `test_research_agent`,
`test_evidence_ranker`, `test_analyst_agent`, `test_verdict_agent`,
`test_citation_verifier`, `test_verify_route`, `test_pipeline`). Deterministic
tests are consistently green. Live tests (need `FASTROUTER_API_KEY` /
`TAVILY_API_KEY` / Supabase creds) are generally green but have shown occasional,
well-understood non-determinism: the citation-safety-net triggering on a
claim-dependent basis, and (before today's timeout fix) the occasional 600s hang.
Tests that hit `VerdictCitationError` as a live outcome treat it as an accepted
result alongside a correct verdict, not a failure — see the tests' own comments
for why.

**`eval/run_eval.py`** (22 hand-written cases spanning all 7 labels): pass rate has
ranged **64–73%** across runs on identical code, due to genuine LLM output
variance, not regressions between runs. Not yet compared against the PRD's
eventual 500-case/82% target — this is Sprint 11's starter baseline, expected to
grow. Of the recurring failures, roughly 5/8 trace to the residual audit pattern
above (§7), 2/8 to the MISLEADING/FALSE calibration question, 1/8 to the
OUTDATED/FALSE ambiguity.

---

## 9. Deviations from the sprint prompts / functional spec (cumulative)

- **All LLM calls routed through FastRouter**, not `api.openai.com` directly —
  explicit user decision, post-Sprint 2.
- **Sprint 6:** also parallelized the 3–5 queries *within* one claim's research,
  not just claim-vs-claim (spec only asked for the latter).
- **Sprint 7:** Analyst Agent takes `Claim` + `EvidencePackage`, not just
  `EvidencePackage` as the spec's table says — needed the claim text to do its job.
- **Post-Sprint 7 retrofit:** `AnalystOutput.fringe_vs_consensus_note` and its
  detection logic removed entirely; evidence-balance scoring now lives solely in
  Evidence Ranker's `confidence_raw`.
- **Sprint 8:** Verdict Agent takes `Claim` + `AnalystOutput` + `EvidencePackage`,
  not just `Claim` + `AnalystOutput` — needed real source URLs to cite, which
  `AnalystOutput` (prose only) doesn't carry.
- **Sprint 8 rebuilt twice post-shipping** (see top-of-file summary): heuristic +
  hardcoded exempt-phrase list → heuristic + `gpt-4o-mini` semantic classifier →
  current structured self-declared segments. Each rebuild targeted a specific,
  live-testing-demonstrated false-positive class in the previous version.
- **Sprint 9:** Citation Verifier's "fetch failure is inconclusive, not a failure"
  design isn't in the spec — discovered via live testing that Wikipedia/Reuters
  block automated fetches outright.
- **Sprint 10:** `VerifyResponse` gained a `message` field not in the original
  spec; `GET /verdicts/{submission_id}` was never built (spec'd but out of
  Sprint 10's actual scope); `intent_classifier.py`/`claim_extractor.py` were
  converted from sync to async (they predated Sprint 6's async conversion) —
  required for correctness once wired into a real async route, not optional.
- **Today:** `llm_client.py` given an explicit 60s request timeout (SDK default
  was 600s) — not part of any sprint prompt, found while diagnosing a live test
  anomaly during the structured-segments retrofit.
