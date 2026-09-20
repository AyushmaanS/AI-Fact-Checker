# Current State
**Last updated:** 2026-09-20 · after Sprint 15 + 4 Phase-1 retrofits · commit `9f97c33`

Living snapshot of what's actually true in the code right now. The other docs in
this folder (`prd-v1-draft.md`, `phase1-2-functional-spec.md`,
`phase1-2-engineering-requirements.md`, `phase1-2-sprint-plan.md`) describe intent
and history; this one describes reality, and should be updated whenever reality
and those diverge.

---

## 1. What's implemented

**Phase 1 (Sprints 0–11): done.** Phase 2 (Sprints 12–18, video/image ingestion):
**started** — Sprints 12–16 done, 17–18 not started.

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
| 14 | `app/ingestion/media_downloader.py` (yt-dlp) + `POST /verify/url` (ingestion only — see §9) |
| 15 | `app/ingestion/video_path.py` audio portion (ffmpeg + Whisper transcription) — standalone, not wired in yet (see §9) |
| 16 | `app/ingestion/video_path.py` frame sampling + GPT-4o Vision (`analyze_frames`) — standalone, not wired in yet (see §9) |

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
`None` (no transcription until Sprint 15+). See §9 for how this compares to
functional-spec §B.4's eventual full contract.

**`POST /verify/url` is ingestion-only too, and deliberately doesn't build a
`StructuredContentObject` at all.** It resolves the URL (Sprint 12), attempts a
download (`media_downloader.download_media`), persists a `submissions` row, and
returns one of three canned messages depending on outcome (downloaded /
caption-only fallback / clean failure) — no `claims`/`verdicts`. Object assembly
for a URL submission is left to Sprint 17 ("Media Router + Object Assembly"),
which is explicitly where the spec says that job belongs; building one here would
have been scope creep with nothing to consume it yet.

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
- **Media Downloader (Sprint 14) never raises — a private/deleted/rate-limited
  post is a normal outcome, not an exceptional one** (spec §B.5). Two-stage
  attempt: full download first; on any failure, one metadata-only (no video)
  fetch as a second chance at just the caption, since some failure modes (e.g. a
  CDN-specific rate limit) leave a post's metadata reachable even when the video
  itself isn't. Confirmed live against a real public Instagram Reel (from
  `@instagram`'s own account) and a deliberately nonexistent one — see §8.
- **`submissions.input_type` only allows `'text' | 'upload' | 'url'`** (real
  Postgres check constraint, `app/db/schema.sql`) — not a media-type distinction.
  Found live while building Sprint 14: both `POST /verify/upload` and the new
  `POST /verify/url` had been passing made-up values (`"video_upload"`,
  `"video_url"`) that violate this constraint. `/verify/upload` had shipped in
  Sprint 13 with this bug already in it, invisible the whole time because its
  tests mock `insert_submission` and never exercised the real constraint — only
  caught now because Sprint 14's own live check happened to hit the real DB.
  Both fixed to `"upload"` / `"url"`.
- **`ffmpeg` isn't installed system-wide — `imageio-ffmpeg` (a pip package
  bundling a portable static binary) is used instead**, deliberately, over a
  system-level install (`winget` is available and would have worked, but a
  system-wide install reaches outside this project the way none of its other
  dependencies do - `imageio_ffmpeg.get_ffmpeg_exe()` keeps ffmpeg fully
  contained in the venv like everything else). Confirmed working live
  (ffmpeg 7.1) before writing any code against it.
- **FastRouter proxies Whisper too, not just chat models** — confirmed live
  (`openai/whisper-1`, real `audio.transcriptions.create()` call, real
  `verbose_json` response with per-segment `no_speech_prob`/`avg_logprob`).
  This settles a question this doc used to carry as open: `OPENAI_API_KEY` is
  **not** needed for transcription after all - see §6.
- **Transcript confidence uses `no_speech_prob` OR `avg_logprob` together, not
  `no_speech_prob` alone** (`video_path._is_low_confidence`) - flags on ANY
  segment tripping either signal, not an average across segments. Found live,
  the hard way: a pure 440Hz tone (standing in for "music, no speech") got
  hallucinated by Whisper into real-looking short text - `"**BLEEP**"` one run,
  `"[(12-Bell Sounds)]"` and `"the"` on others - each time with
  `no_speech_prob` around 0.44, *under* a naive 0.5 threshold, so a
  no_speech_prob-only check would have called it confident speech. `avg_logprob`
  caught it every time (-1.8ish vs. -0.3 for genuine speech, confirmed live on
  a real transcript). One hallucinated segment among otherwise-good ones still
  trips the flag - not averaged away - since a fabricated "fact" threaded into
  real speech is exactly what a fact-checking product can't afford to trust
  silently. See §8 for the live verification.
- **Frame sampling batches all selected frames into ONE GPT-4o Vision call**,
  not one call per frame (`video_path.analyze_frames`) - cheaper, and lets the
  model reason across frames as a sequence rather than describing each in
  isolation. Frames are sampled at 1fps (ffmpeg `fps=1`); videos longer than
  `MAX_FRAMES_TO_SEND` (10) seconds are evenly downsampled to roughly that many
  frames rather than sent in full, to control cost on longer videos - a short
  clip just sends every frame.
- **Vision output is structured (`label` + `on_screen_text` via
  `chat.completions.parse`), not a single free-text description** - same
  pattern as every other LLM call in this codebase. The system prompt
  explicitly instructs the model not to invent on-screen text that isn't
  visible; confirmed live it reads real text accurately rather than
  hallucinating - a burned-in "SALES UP 47 PERCENT" overlay came back
  transcribed exactly, and a separate video's genuine built-in frame-counter
  digits (an unintended discovery - ffmpeg's `testsrc` pattern draws a visible
  running counter) were also read correctly rather than something being
  invented for a plain background. See §8.

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
| `POST /verify/url` | ✅ implemented, ingestion-only (Sprint 14) — resolves the URL, attempts a yt-dlp download, falls back to caption-only or a clean failure message; see §1 |

---

## 6. Environment variables (`.env`)

| Var | Used by | Status |
|---|---|---|
| `FASTROUTER_API_KEY` | all LLM calls | active |
| `TAVILY_API_KEY` | research_agent | active (on a third key as of 2026-09-20 — both the original and the one pre-authorized backup hit `ForbiddenError: usage limit` during this session; project owner supplied a new key to unblock the eval rerun in §8) |
| `SUPABASE_URL` / `SUPABASE_KEY` | db/client.py | active |
| `OPENAI_API_KEY` | — | **present in `.env.example` but unused, confirmed not needed** — FastRouter proxies Whisper too (`app/ingestion/video_path.py`, Sprint 15), same as every other model in this codebase. No code path calls OpenAI directly. |

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

**111 tests collected** across 16 test files (`test_main`, `test_schemas`,
`test_db`, `test_intent_classifier`, `test_claim_extractor`, `test_research_agent`,
`test_evidence_ranker`, `test_analyst_agent`, `test_verdict_agent`,
`test_citation_verifier`, `test_verify_route`, `test_pipeline`, `test_url_resolver`,
`test_caption_path`, `test_media_downloader`, `test_video_path`). The 10
`test_url_resolver` tests are Sprint 12's; 5 in `test_caption_path` plus 5
upload-endpoint tests in `test_verify_route` are Sprint 13's; 4 in
`test_media_downloader` plus 4 url-endpoint tests in `test_verify_route` are
Sprint 14's; 12 in `test_video_path` are Sprint 15's and 12 more in the same
file are Sprint 16's — all 52 deterministic, no network, no live-key gating
(Sprints 14/15/16's real yt-dlp/ffmpeg/Whisper/Vision/DB behavior was instead
verified with real, ad-hoc live checks, not baked into the permanent suite -
see the note below on why). **All pass**, including every live test
(needs `FASTROUTER_API_KEY` / `TAVILY_API_KEY` / Supabase creds) — confirmed via
two full consecutive runs after retrofit 4, the second one clean, plus the
non-live subset (106 tests) confirmed clean again after Sprint 16, most recently
106 passed / 5 deselected (one flaky live-marked test not caught by that filter,
see below). One live test's own expectation had to be fixed along
the way, during retrofit 4: the old
"per-URL" citation removal test asserted a shared citation gets fully wiped when
any one of its lines fails; that's no longer correct under the new fine-grained
per-`EvidenceLine` removal (§3), so the assertion was corrected and a deterministic
regression test for the same "shared URL, one valid line" case was added
(`test_shared_citation_url_survives_if_one_of_its_lines_is_valid`) — not a code bug,
a stale test expectation caught by live testing doing exactly its job.

**A reminder for next time: `-k "not live"` only filters by test *name*, not by
`@LIVE_SKIP`.** Running the suite this way during Sprint 15 still hit a real
live call in `test_verdict_agent.py::test_verdict_true_for_well_supported_claim`
(no "live" in its name) and it came back `PARTIALLY_TRUE` instead of the
tolerated `{TRUE, UNVERIFIABLE}` — re-ran in isolation and it passed
(`TRUE`), confirming ordinary LLM judgment variance on Phase 1's Verdict Agent,
unrelated to Sprint 15 (which touches only `app/ingestion/video_path.py` and
adds one unused-elsewhere constant to `llm_client.py`). Not a regression; just a
reminder that "not live" by keyword isn't the same as "no network calls."

**Sprints 14, 15, and 16's live verification were all deliberately kept out of
the permanent pytest suite**, unlike every other live test here, though for two
different reasons depending on the sprint. Sprint 14: a genuine
content-liveness risk none of the other live tests carry - it would depend on
one specific, currently-real Instagram Reel URL staying up indefinitely (the
others depend on stable facts like "the Eiffel Tower was completed in 1889,"
not on a specific post not being deleted). Sprints 15/16: no content-liveness
risk at all (all test media is generated locally, deterministically) - kept out
simply because a real Whisper/Vision API call on every test run is slower and
costs real money for something the mocked tests already cover logically; the
live check's job was to prove the mechanism works against reality once, not to
re-prove it on every future run. Verified instead via ad-hoc live runs during
development. Sprint 14: a real Reel from `@instagram`'s own account (found by
browsing, not fabricated) downloaded successfully end-to-end through the
actual `/verify/url` route with a real Supabase write (3.28MiB file, real
extracted caption); a deliberately nonexistent reel id (`.../reel/AAAAAAAAAAA/`)
failed cleanly through the same route with no crash. This same live check is
what caught the `input_type` bug above. Sprint 15: a real Windows-TTS-generated
speech clip and an ffmpeg-generated pure-tone clip, each muxed into a real
video file and run through the actual `transcribe_video()` end to end (not
mocked) - see §3 for what the tone clip caught. Sprint 16: a real
ffmpeg-`drawtext`-generated video with a burned-in "SALES UP 47 PERCENT"
overlay, run through the actual `analyze_frames()` end to end - the on-screen
text came back transcribed exactly, confirming the DoD directly.

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
- **Sprint 14:** same ingestion-only scoping as Sprint 13, for the same reason
  (this sprint's DoD is the download mechanism itself, not fact-checking) —
  `POST /verify/url` doesn't build a `StructuredContentObject` at all, unlike
  Sprint 13's upload endpoint, since that job is explicitly Sprint 17's per its
  own title ("Media Router + Object Assembly"); see §1. Also found and fixed a
  real bug retroactively affecting Sprint 13 too: `submissions.input_type` has a
  real Postgres check constraint (`'text' | 'upload' | 'url'` only,
  `app/db/schema.sql`), and both upload endpoints had been passing invented
  values (`"video_upload"`, `"video_url"`) that violate it - invisible until
  Sprint 14's own live DB check happened to hit the real constraint, since every
  prior test for both endpoints mocked `insert_submission`. See §3/§8.
- **Sprint 15:** `transcribe_video()` is a standalone function with no caller
  anywhere in the codebase yet - same as Sprint 12's `resolve_url()` (§1), and
  for the same reason: nothing has assembled a full ingestion pipeline that
  would call it (Sprint 17's job). Also: the prompt's suggested confidence
  heuristic ("no_speech_prob... or transcript length near zero") turned out to
  need a real fix once tested live - see §3's write-up of the hallucinated
  "**BLEEP**"/tone-clip finding and why `avg_logprob` had to be added alongside
  `no_speech_prob`, not used as originally scoped.
- **Sprint 16:** `analyze_frames()` is also a standalone function with no
  caller yet, same reason as Sprint 15. Sends all selected frames in one
  `chat.completions.parse` call rather than one call per frame - the prompt's
  "batch a reasonable subset... to control cost" read as one batched call being
  the point, not literally separate per-frame calls. `MAX_FRAMES_TO_SEND = 10`
  and the evenly-strided downsampling formula are this session's own choice,
  generalizing the prompt's literal "every 3rd frame" example to scale
  sensibly for both short and long videos rather than hardcoding a fixed
  stride.
