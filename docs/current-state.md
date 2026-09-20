# Current State
**Last updated:** 2026-09-20 · after Sprint 11 + 4 post-hoc retrofits · commit `16328b9`

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
| 5 | Evidence Ranker + credibility weighting (rebuilt once since — see §9) |
| 6 | asyncio concurrency (research), later extended to the full per-claim chain |
| 7 | Analyst Agent (rebuilt twice since — see §9) |
| 8 | Verdict Agent + citation enforcement (rebuilt three times since — see §9) |
| 9 | Citation Verifier (updated once since — see §9) |
| 10 | Response Formatter + full pipeline wiring (`POST /verify`) (updated once since — see §9) |
| 11 | Eval set (22 hand-written cases) + `eval/run_eval.py` |

Plus 4 retrofits not tied to a sprint number, each shipped after the sprint that
introduced the thing it replaced, each verified live before landing (details in §9):
1. Fringe-vs-consensus threshold → capped weighted-evidence-score
2. Free-text rationale + heuristic-only validator → two-stage validator (heuristic + LLM semantic check)
3. Two-stage validator → structured self-declared `RationaleSegment`s
4. Structured segments → **evidence lines + one audited summary line** (current — see §2/§3)

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
| `POST /verify/upload`, `POST /verify/url` | Phase 2, not started |

---

## 6. Environment variables (`.env`)

| Var | Used by | Status |
|---|---|---|
| `FASTROUTER_API_KEY` | all LLM calls | active |
| `TAVILY_API_KEY` | research_agent | **currently on the backup key, and that key is now ALSO quota-exhausted** (`ForbiddenError: This request exceeds your plan's set usage limit`, hit 2026-09-20 running the Sprint 11 eval rerun) — see §7/§8. No further pre-authorized backup key exists. |
| `SUPABASE_URL` / `SUPABASE_KEY` | db/client.py | active |
| `OPENAI_API_KEY` | — | **present in `.env.example` but unused** — no code path calls OpenAI directly; reserved for Whisper transcription once Phase 2 starts, may or may not still be needed depending on whether FastRouter proxies audio endpoints (never checked) |

---

## 7. Known bugs / tech debt

- **Tavily quota exhausted on both keys (new, blocking, 2026-09-20).** The original
  key hit its plan limit during Sprint 11; the one pre-authorized backup key has now
  also hit its limit, surfaced while re-running the Sprint 11 eval set after retrofit
  4. No code path handles this gracefully (see the next bullet) and no further
  backup key is available — needs a decision from the project owner (new key, wait
  for reset, or upgrade plan) before `eval/run_eval.py` or any live Tavily-dependent
  test can run again.
- **No per-claim error isolation in `pipeline.process_claims`, still true.**
  `_process_one_claim` no longer wraps anything in a try/except at all (`produce_verdict`
  itself no longer raises, so there was nothing left there to catch) — but that
  never covered this gap anyway. Any exception from `research_claim`,
  `rank_evidence`, `analyze_evidence`, or `verify_citations` still propagates out of
  `asyncio.gather` and kills the *entire* concurrent batch, not just the one claim.
  This is exactly what the Tavily quota error above did to the eval rerun — the
  very first `ForbiddenError` in the first batch took down all 22 cases at once,
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
  sentence.
- **MISLEADING vs FALSE calibration, OUTDATED vs FALSE ambiguity:** both open
  questions carried over from the prior design's eval runs (absolute/totalizing
  claims skewing FALSE over MISLEADING; genuine taxonomy-boundary overlap on
  "was X, now isn't" claims). Not re-confirmed against retrofit 4's actual output
  yet — the eval rerun needed to check this is exactly what's blocked by the Tavily
  quota issue above.
- **`GET /verdicts/{submission_id}`** not implemented (see §5).

---

## 8. Test / eval status

**59 tests collected** across 12 test files (`test_main`, `test_schemas`,
`test_db`, `test_intent_classifier`, `test_claim_extractor`, `test_research_agent`,
`test_evidence_ranker`, `test_analyst_agent`, `test_verdict_agent`,
`test_citation_verifier`, `test_verify_route`, `test_pipeline`). **All 59 pass**,
including every live test (needs `FASTROUTER_API_KEY` / `TAVILY_API_KEY` / Supabase
creds) — confirmed via two full consecutive runs after retrofit 4, the second one
clean. One live test's own expectation had to be fixed along the way: the old
"per-URL" citation removal test asserted a shared citation gets fully wiped when
any one of its lines fails; that's no longer correct under the new fine-grained
per-`EvidenceLine` removal (§3), so the assertion was corrected and a deterministic
regression test for the same "shared URL, one valid line" case was added
(`test_shared_citation_url_survives_if_one_of_its_lines_is_valid`) — not a code bug,
a stale test expectation caught by live testing doing exactly its job.

**`eval/run_eval.py`: not yet re-run to completion post-retrofit-4.** The rerun was
started and immediately hit the Tavily quota exhaustion described in §6/§7 on the
very first batch — zero cases completed, not a partial or degraded result. The prior
design's baseline (22 hand-written cases spanning all 7 labels) ranged **64–73%**
pass rate across multiple runs on stable code; that baseline is what retrofit 4
still needs to be checked against once Tavily access is restored. This is the one
piece of the user's explicit retrofit-4 request ("re-run the eval set in full... and
confirm no regressions") not yet completed — everything else (all 7 files rebuilt,
all tests updated/passing, docs updated) is done.

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
- **Today (2026-09-20):** eval rerun blocked on Tavily quota exhaustion on both the
  original and backup keys — see §6/§7/§8.
