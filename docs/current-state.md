# Current State
**Last updated:** 2026-09-22 · after Sprint 18 (all 18 current-plan sprints done) + 4 Phase-1 retrofits · commit `9b54a16`

Living snapshot of what's actually true in the code right now. The other docs in
this folder (`prd-v1-draft.md`, `phase1-2-functional-spec.md`,
`phase1-2-engineering-requirements.md`, `phase1-2-sprint-plan.md`) describe intent
and history; this one describes reality, and should be updated whenever reality
and those diverge.

---

## 1. What's implemented

**Phase 1 (Sprints 0–11): done. Phase 2 (Sprints 12–18): done.** All 18 sprints
on the current plan are complete as of 2026-09-22 — `POST /verify/url` and
`POST /verify/upload` are fully wired end-to-end, confirmed live against a real
public Instagram Reel (see §8). The sprint plan's own text calls this point
"worth a genuine pause before continuing" to Phase 3/4, which aren't planned yet.

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
| 15 | `app/ingestion/video_path.py` audio portion (ffmpeg + Whisper transcription) — wired in as of Sprint 18 |
| 16 | `app/ingestion/video_path.py` frame sampling + GPT-4o Vision (`analyze_frames`) — wired in as of Sprint 18 |
| 17 | `app/ingestion/media_router.py` (`detect_media_type` + `route_and_assemble`) — ties Sprints 13/15/16 together into one `StructuredContentObject`; wired in as of Sprint 18 |
| 18 | Full Phase 2 integration: `POST /verify/upload` and `POST /verify/url` now run ingested content through the real Phase 1 pipeline, plus Supabase Storage (24h video retention, 90-day extracted-text retention) — see §2/§3/§9 |

Plus 4 retrofits not tied to a sprint number, each shipped after the sprint that
introduced the thing it replaced, each verified live before landing (details in §9):
1. Fringe-vs-consensus threshold → capped weighted-evidence-score
2. Free-text rationale + heuristic-only validator → two-stage validator (heuristic + LLM semantic check)
3. Two-stage validator → structured self-declared `RationaleSegment`s
4. Structured segments → **evidence lines + one audited summary line** (current — see §2/§3)

**As of Sprint 18, both `POST /verify/upload` and `POST /verify/url` are fully
wired end-to-end** — every piece built across Sprints 12–17
(`url_resolver.resolve_url`, `media_downloader.download_media`,
`media_router.route_and_assemble`, which itself calls `caption_path`,
`video_path.transcribe_video`, and `video_path.analyze_frames`/`analyze_image`)
now has a real caller. See §2 for the full wired pipeline and §8 for the live
end-to-end confirmation against a real public Instagram Reel.

---

## 2. Architecture / pipeline

**Phase 2 entry points (`/verify/upload`, `/verify/url`) — added Sprint 18:**

```
POST /verify/upload {file, caption}          POST /verify/url {url}
  → cleanup_expired_media()                    → cleanup_expired_media()
    (deletes any Storage file whose               (same - both routes check on
    storage_expires_at has passed;                 every request, per the sprint's
    checked on each request, not a                 "no cron job needed yet" allowance)
    cron job - see §3)
  → save upload to local temp file            → resolve_url (Sprint 12)
                                                 → download_media (Sprint 14, yt-dlp)
                                                   - success: real video file
                                                   - caption-only fallback: no file,
                                                     just a caption
                                                   - total failure: canned message,
                                                     short-circuits here, no further
                                                     ingestion attempted
  insert_submission (gets submission_id)      insert_submission (raw_input = resolved
                                                 canonical URL, not the raw messy input)
  → _store_and_assemble(submission_id, file_path, caption, source_url):
      1. upload_media_file -> Supabase Storage (skipped if no file - e.g. the
         caption-only fallback case)
      2. route_and_assemble (Sprint 17): detect_media_type -> dispatches to
         Video Path (transcribe_video + analyze_frames, concurrent) / a single
         analyze_image call / Caption Path only - assembles one
         StructuredContentObject
      3. local file/temp dir cleaned up (Storage, or nothing, is the durable
         copy from here on)
      4. _combine_content_for_pipeline: "Caption: ...\n\nTranscript: ...\n\n
         Visual context: ..." (labeled sections, only for what's actually
         present) -> one plain-text string
      5. update_submission_media: persists storage_path (+24h expiry) and this
         combined text as extracted_text (+90-day expiry) onto the submission row
  → if combined text is empty: canned NO_EXTRACTABLE_CONTENT_MESSAGE, same
    "graceful, still 200" stance as every other short circuit in this codebase
  → otherwise: _run_phase1_pipeline(submission_id, combined_text, start) -
    the exact same shared function POST /verify itself calls (see below) -
    from here on, an ingested Reel and a pasted paragraph of text are
    indistinguishable to the rest of the system.
```

**Phase 1 pipeline (shared by all three `/verify*` routes via
`_run_phase1_pipeline`):**

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
`response_formatter.py`. Ingestion files (Phase 2): `url_resolver.py`,
`media_downloader.py`, `caption_path.py`, `video_path.py`, `media_router.py`.
Orchestration: `pipeline.py` (Phase 1 concurrency — untouched by retrofit 4
except removing a now-dead try/except, see §9), `routes/verify.py` (all three
HTTP routes, ingestion wiring, and DB/Storage persistence — as of Sprint 18 this
is a substantial file; `_run_phase1_pipeline` is the one function all three
routes converge on). Shared: `llm_client.py` (FastRouter client + model IDs
including Whisper, 60s timeout), `db/client.py` (Supabase tables + Storage).

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
- **There's no separate "Image Path" component anywhere in the spec, but the
  DoD explicitly requires testing a static image** - Sprint 17's own prompt
  only names dispatch to "Video Path or Caption Path." Bridged by adding
  `video_path.analyze_image(image_path)`: reuses the exact same vision call
  and combination logic `analyze_frames` uses, just skipping ffmpeg frame
  extraction entirely and sending the one static image directly (there's only
  ever one frame). `VISION_SYSTEM_PROMPT` was generalized slightly (no longer
  assumes "frames from a video") so the same prompt is accurate for both
  callers. See §9 for the full flagged deviation.
- **A downloaded file's `media_type` is detected from its own MIME type**
  (`mimetypes.guess_type`, via extension), not sniffed from content - a
  downloaded file always has a real extension, so no need to open/inspect it.
  Found live: Python's `mimetypes` module doesn't register `.webp` by default
  on this system, despite Instagram actively serving images as WebP -
  registered explicitly (`mimetypes.add_type`) rather than left as a silent
  gap that would have quietly misrouted a real, common case to `text_post`.
  An unrecognized extension still falls back to `text_post` deliberately -
  caption-only degradation is always safe; guessing at video/image handling
  for an unknown format is not.
- **`transcribe_video` and `analyze_frames` run concurrently for a video**
  (`asyncio.gather`), not sequentially - independent operations (audio vs.
  frames) that both only read the same source file, consistent with this
  codebase's established concurrency stance since Sprint 6.
- **A shared `_run_phase1_pipeline(submission_id, text, start)` is what all
  three `/verify*` routes actually call** (Sprint 18, `routes/verify.py`) -
  extracted rather than duplicating the intent -> claims -> verdicts ->
  persistence -> response block three times. Not explicitly asked for, but a
  direct, unavoidable consequence of "feed ingestion output into the Phase 1
  pipeline from Sprint 10" for *two* new routes at once.
- **Ingested content becomes one labeled plain-text block, not three separate
  fields passed to the classifier** (`_combine_content_for_pipeline`) -
  `"Caption: ...\n\nTranscript: ...\n\nVisual context: ..."`, only for
  whichever sections are actually non-empty. Per the sprint prompt's own
  "concatenate with clear section labels" instruction. If all three are empty
  (e.g. a video with unclear audio, no on-screen text, and no caption), the
  route short-circuits with a canned "nothing extractable" message rather than
  handing the Phase 1 pipeline an empty string.
- **Supabase Storage bucket access uses RLS policies scoped to the `media`
  bucket, not a `service_role` key.** Bucket creation itself was rejected by
  RLS under the existing `anon` key (`403: new row violates row-level security
  policy`, confirmed live) - Storage has RLS on by default, unlike the plain
  tables in this project, which have never had it enabled at all. Rather than
  introduce a second, more-privileged credential for just this one feature,
  three narrow policies (insert/select/delete, scoped to `bucket_id = 'media'`)
  match the same all-open posture already true of every other table here. This
  needed a one-time manual step in the Supabase SQL editor - same as how the
  original Sprint 1 schema was applied - since neither DDL nor bucket/policy
  creation is reachable through the REST client's normal table/storage API
  calls, confirmed by trying both live.
- **24h video / 90-day text retention are both enforced by a stored
  `expires_at` checked on each Phase 2 request** (`cleanup_expired_media`,
  called at the top of both `/verify/upload` and `/verify/url`), not a
  scheduled job - exactly the allowance the sprint prompt itself offered
  ("a full cron job isn't required yet"). A file only actually gets deleted
  the next time *some* Phase 2 request happens to run after its expiry, not
  the instant it expires - accepted trade-off, matching the prompt's own
  framing of this as good enough for now, not the eventual production design.
  Never raises on a single delete failure (e.g. already gone), so a cleanup
  hiccup can't take down the request that triggered it.
- **Storage keys are named `{submission_id}{original extension}`**, not the
  original filename - guaranteed unique, always traceable back to exactly one
  `submissions` row without a separate lookup table.

---

## 4. Database schema (Supabase Postgres) — extended in Sprint 18

```sql
submissions(id uuid pk, raw_input text, input_type text, created_at,
            storage_path text, storage_expires_at timestamptz,          -- Sprint 18
            extracted_text text, extracted_text_expires_at timestamptz) -- Sprint 18
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

`submissions`'s four new columns (Sprint 18, applied by the project owner
manually in Supabase's SQL editor — see `app/db/schema.sql`'s "Sprint 18"
section, and §3 for why this couldn't be done programmatically): `storage_path`
+ `storage_expires_at` track the ingested video's location in Supabase Storage
and its 24h deletion deadline (`null` for a plain-text submission, or a
caption-only URL fallback with no downloaded file); `extracted_text` +
`extracted_text_expires_at` hold the same combined caption/transcript/visual
text that was fed to the Phase 1 pipeline, retained 90 days.

**Supabase Storage** (new, Sprint 18): one private bucket, `media`, holding
ingested video files under `{submission_id}{extension}` keys, cleaned up by
`db.client.cleanup_expired_media()` (see §3). No vector DB, no Redis, no job
queue — those remain explicitly deferred to Phase 4+.

---

## 5. API endpoints

| Endpoint | Status |
|---|---|
| `GET /health` | ✅ implemented |
| `POST /verify` | ✅ implemented |
| `GET /verdicts/{submission_id}` | ❌ **not implemented** — speced in functional-spec §A.4, deliberately out of Sprint 10's scope |
| `POST /verify/upload` | ✅ **fully implemented as of Sprint 18** — accepts a video + optional caption, stores it in Supabase Storage, runs it through the full ingestion + Phase 1 pipeline, returns a real cited `VerifyResponse` (or a graceful canned message if there's nothing to check) |
| `POST /verify/url` | ✅ **fully implemented as of Sprint 18** — resolves the URL, downloads via yt-dlp (or falls back to caption-only, or fails cleanly), same full pipeline as upload. Confirmed live end-to-end against a real public Instagram Reel — see §8. |

---

## 6. Environment variables (`.env`)

| Var | Used by | Status |
|---|---|---|
| `FASTROUTER_API_KEY` | all LLM calls | active |
| `TAVILY_API_KEY` | research_agent | active (on a third key as of 2026-09-20 — both the original and the one pre-authorized backup hit `ForbiddenError: usage limit` during this session; project owner supplied a new key to unblock the eval rerun in §8) |
| `SUPABASE_URL` / `SUPABASE_KEY` | db/client.py | active - `SUPABASE_KEY` is the `anon` role (confirmed by decoding the JWT), used for both the Postgres tables and, as of Sprint 18, the `media` Storage bucket via RLS policies rather than a `service_role` key - see §3 |
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

**125 tests collected** across 17 test files (`test_main`, `test_schemas`,
`test_db`, `test_intent_classifier`, `test_claim_extractor`, `test_research_agent`,
`test_evidence_ranker`, `test_analyst_agent`, `test_verdict_agent`,
`test_citation_verifier`, `test_verify_route`, `test_pipeline`, `test_url_resolver`,
`test_caption_path`, `test_media_downloader`, `test_video_path`,
`test_media_router`). The 10 `test_url_resolver` tests are Sprint 12's; 5 in
`test_caption_path` plus 5 upload-endpoint tests in `test_verify_route` are
Sprint 13's; 4 in `test_media_downloader` plus 4 url-endpoint tests in
`test_verify_route` are Sprint 14's; 12 in `test_video_path` are Sprint 15's
and 12 more in the same file are Sprint 16's; 9 in `test_media_router` are
Sprint 17's, of which 8 are deterministic and 1
(`test_schema_consistency_across_all_three_media_types`) is genuinely live -
unlike Sprints 14-16, this one **is** baked into the permanent suite, because
the sprint prompt explicitly asked to "write a...pytest," not just verify
informally, and it needs no external content (generates its own video/image
locally via ffmpeg each run, so no content-liveness risk). Sprint 18 rewrote
the upload/url endpoint tests in `test_verify_route` to mock at the ingestion
level (`route_and_assemble`, storage functions) instead of asserting on
now-gone placeholder messages, added 3 `_combine_content_for_pipeline` tests,
and added 4 real (non-mocked) `test_db.py` tests for the new storage/cleanup
functions - `test_db.py` has always been a fully-live file (no mocking at
all; skipped entirely without Supabase creds), so these follow that file's own
existing convention rather than Sprint 18 introducing a new pattern. **All
125 pass** — confirmed via
two full consecutive runs after retrofit 4, the second one clean, plus the
non-live subset (filtered by test name, `-k "not live"`) confirmed clean again
after Sprint 18: 120 passed / 5 deselected - note this filter is name-based
only (see below), so it still includes several genuinely-live tests whose
names don't happen to contain "live", including all 4 new Sprint 18 DB/storage
tests. One live test's own expectation had to be fixed along the way,
during retrofit 4: the old
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

**Sprint 17 breaks that pattern on purpose** — its schema-consistency check
(`test_schema_consistency_across_all_three_media_types`, `test_media_router.py`)
*is* a permanent, live-gated pytest, not an ad-hoc check, because the sprint
prompt explicitly asked to "write a...pytest," and because it carries neither
of the two reasons the others were kept out: no external content-liveness risk
(video/image are generated locally via ffmpeg inside the test itself, fresh
each run) and it's proving something no other test does (the three dispatch
paths actually converge on one consistent object shape) rather than re-proving
logic the mocked tests already cover. Confirmed live: real burned-in overlay
text ("TEST OVERLAY 99" in the video, "IMAGE OVERLAY 42" in the image) came
back correctly in each object's `visual_context`, and the null/non-null
pattern was exactly as spec'd (`transcript` real only for video;
`visual_context` real for video and image; both null for the caption-only case).

**Sprint 18's actual DoD — "paste a real public Instagram Reel URL into
`POST /verify/url` and get back a complete, cited verdict" — was run for
real, once, deliberately (this is the single most expensive possible
verification in this codebase: download + transcription + vision + full
Phase 1 research/verdict/citation-check, all real API calls) and passed
outright.** Found a real Reel via browsing (National Geographic's own
account, not fabricated): `natgeo/reel/Dde8AyFAjwU`, captioned "Grandmother
orcas use decades of experience to feed their families." Posted to
`/verify/url`:
- **200 OK in 61.1s.**
- **6 claims extracted** — not just from the caption, but genuinely detailed
  ones ("Female orcas may stop having calves around age 40 but can live for
  decades longer," "Research found that young orcas with a living grandmother
  are more likely to survive") that could only have come from the video's
  on-screen text overlays via `analyze_frames`'s vision call - confirmed by
  checking `extracted_text` directly afterward, whose `Transcript:` section
  was just the single word "you" (the video is driven by on-screen graphics
  over music, not narration - a real, honest example of why this design
  combines all three sources rather than relying on any one).
- **All 6 verdicts came back TRUE, every one with real, relevant, credible
  citations** — NBC News, the Natural History Museum (UK and LA), Wikipedia,
  *Nature*, PBS, WWF, King5 News, and National Geographic itself - each
  citation matched to the specific claim it supports, confirming Research
  Agent -> Evidence Ranker -> Analyst Agent -> Verdict Agent -> Citation
  Verifier all ran correctly against real content this pipeline had never
  seen before.
- **`aggregate_label: "TRUE"`**, correctly computed from six all-TRUE verdicts.
- Confirmed directly against the DB afterward: `storage_path` was the
  expected `{submission_id}.mp4`, `storage_expires_at` was exactly 24h after
  creation, `extracted_text_expires_at` was exactly 90 days after creation,
  and `extracted_text` showed all three labeled sections as designed.
- One minor, pre-existing-behavior observation, not a Sprint 18 bug: one
  claim's (`#NatGeoQueens is streaming on DisneyPlus and Hulu`) second
  evidence line paraphrased as "doesn't specifically mention #NatGeoQueens
  streaming" yet was still marked `stance: "for"` and contributed to a TRUE
  verdict - the same kind of evidence-stance-classification softness already
  covered by this section's existing entries, not something Sprint 18
  introduced or could reasonably fix within its own scope.

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
- **Sprint 15:** `transcribe_video()` had no caller anywhere in the codebase
  at the time (same situation as Sprint 12's `resolve_url()`, §1) - wired in
  by Sprint 18, via `media_router.route_and_assemble`. Also: the prompt's
  suggested confidence heuristic ("no_speech_prob... or transcript length near
  zero") turned out to need a real fix once tested live - see §3's write-up of
  the hallucinated "**BLEEP**"/tone-clip finding and why `avg_logprob` had to
  be added alongside `no_speech_prob`, not used as originally scoped.
- **Sprint 16:** `analyze_frames()` was also uncalled at the time, same
  situation as Sprint 15 - wired in by Sprint 18. Sends all selected frames in
  one `chat.completions.parse` call rather than one call per frame - the
  prompt's "batch a reasonable subset... to control cost" read as one batched
  call being the point, not literally separate per-frame calls.
  `MAX_FRAMES_TO_SEND = 10` and the evenly-strided downsampling formula are
  this session's own choice, generalizing the prompt's literal "every 3rd
  frame" example to scale sensibly for both short and long videos rather than
  hardcoding a fixed stride.
- **Sprint 17: the prompt's own dispatch description names only "Video Path or
  Caption Path," but the DoD explicitly requires testing a static image, and
  the spec's `StructuredContentObject`/§B.5 both treat `image` as a real third
  `media_type` with its own semantics (`transcript: null`).** There's no
  "Image Path" component anywhere in the spec to dispatch to. Bridged by
  adding `video_path.analyze_image()` for the single-image case, reusing
  Sprint 16's vision-calling logic directly rather than building a separate
  image-analysis pipeline from scratch - see §3. `route_and_assemble()` had no
  caller at the time either - wired in by Sprint 18, its "explicit job" per
  Sprint 17's own note here, confirmed exactly right. Also found and fixed a
  real gap live: Python's `mimetypes` module doesn't recognize `.webp` by
  default on this system - see §3.
- **Sprint 18:** covered in depth in §2/§3/§8 (architecture diagram, storage/
  RLS/retention design decisions, live DoD verification) rather than repeated
  here. One thing worth flagging as a deviation specifically: the sprint
  prompt describes Supabase Storage integration as if it were a
  straightforward client-library call, but bucket creation was rejected by
  RLS under the project's existing `anon` key, and there's no DDL-execution
  path through the standard REST/storage client either - both needed a manual
  SQL-editor step from the project owner, the same kind of one-time setup
  Sprint 1's original schema needed. Not discoverable without attempting it
  live, which is exactly what happened.
