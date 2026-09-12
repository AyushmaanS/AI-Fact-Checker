# PRD: AI Fact-Checker for Instagram
**Status:** Draft v1 · **Author:** Product Management · **Source of truth:** *Building an AI Fact-Checker for Instagram: A Complete Agentic System Design* (Amit.Kumar, Educosys Agentic AI System Design series) + companion architecture notes (`ai-fact-checker-system-design-notes.md`)

---

## 1. Overview

Social feeds surface unverified claims faster than users can check them — the average reel gets ~90 seconds of attention, and manual verification (Googling it) is a multi-step task most people abandon within 30 seconds. This product lets a user forward a suspicious Reel to an Instagram account and receive a structured, source-grounded verdict (TRUE / FALSE / MISLEADING / PARTIALLY TRUE / UNVERIFIABLE) within 2 minutes, at the exact moment they're deciding whether to believe or share it.

Over time, submissions build a personal "media diet" profile — what the user consumes, how accurate it tends to be, and whether their information diet shows echo-chamber characteristics.

**Foundational product constraint (non-negotiable, drives every phase below):** every factual statement in a verdict must be grounded to a specific, independently-verified source URL. We do not ship a system that produces confident, uncited verdicts.

---

## 2. Product Goals

| Goal | Metric |
|---|---|
| Fast, trustworthy verification at the point of consumption | Verdict delivered in **<120s at P95** |
| Verdicts as accurate as professional fact-checkers | **≥85% accuracy** vs. professional fact-checker benchmark |
| Every verdict is defensible, not just plausible | **≥3 independent sources** per verdict; 100% of factual statements cited |
| Product works at real social-media scale | **10,000 submissions/day at launch**, roadmap to **500,000/day** |
| Build a durable second value prop beyond one-off checks | Personal media-diet dashboard drives repeat engagement (Phase 5+) |

## 3. Non-Goals (Product-wide)

- **Not** a general-purpose chatbot or open Q&A assistant — scope is limited to verifying claims in submitted content.
- **Not** a content moderation or takedown tool — we produce information, we do not enforce anything on Instagram itself.
- **Not** proactively scanning a user's feed, stories, or DMs — the system only acts on content a user explicitly forwards/submits.
- **Not** treating opinion, satire, or fictional/creative content as factual claims — the Content Intent Classifier gates this out before any research spend.
- **Not** multi-platform at launch — Instagram is the only ingestion channel through Phase 4; TikTok/YouTube/X are future-roadmap, not in this PRD.

---

## 4. Primary Use Case

A user watching Reels sees a claim that feels off (a stat, a quote, a policy outcome). They forward the Reel to the product's Instagram account (or paste a link into the web app). They get a DM back with a verdict label, a plain-English paragraph, a confidence score, and 2–3 source links, plus a link to a fuller web report. Repeat use over weeks/months populates their media-diet dashboard.

---

## 5. System Architecture Summary

Three planes (full diagrams and box-level detail in the companion notes doc):

- **Input Plane** — Instagram DM / Web Interface / Direct URL → Job Queue (Redis/SQS)
- **Agentic Processing Plane** — 9 agents: Orchestrator, Content Extractor, Claim Extractor, Research Agents (parallel, 1/claim), Analyst Agent, Verdict Agent, Citation Verifier, Response Formatter, Profiling Agent (async)
- **Output + Intelligence Plane** — Instagram DM Reply, Web Dashboard, Verdict Store (Postgres+S3), Profiling Agent output, Observability (Langfuse/Grafana)

Storage: **PostgreSQL** (relational source of truth), **Vector DB** (Pgvector/Qdrant — dedup, echo-chamber clustering, RAG evidence chunks), **Redis** (queue, rate limits, caches), **S3** (video/transcripts/reports/verdict cards).

---

## 6. Success Metrics / KPIs

| Metric | Target |
|---|---|
| P95 end-to-end latency | < 120s |
| Verdict accuracy vs. professional fact-checkers | ≥ 85% |
| Sources per verdict | ≥ 3 independent sources |
| Cost per fact-check | < $0.15 |
| Citation verification fail rate | < 5% |
| DM delivery success rate | > 98% |
| UNVERIFIABLE verdict rate | < 15% (a proxy for research-tool health) |
| Offline eval suite pass rate | ≥ 82% (500-case weekly regression run) |

---

## 7. Phased Release Plan

### **Phase 1 — Core Verification Engine (Text/Caption Input, Web Only)**

**Goal:** Prove the research → adversarial-analysis → cited-verdict loop produces accurate, well-grounded verdicts, independent of video/social-platform complexity.

**Non-Goals:** No video/audio ingestion, no Instagram integration, no media-diet profiling, no dedup caching, no production-grade reliability engineering (basic retries only).

**Engineering Requirements:**
- **Content Intent Classifier** — FACTUAL CLAIM / OPINION / SATIRE·COMEDY / FICTIONAL·CREATIVE / UNRELATED gate, ahead of any research spend.
- **Claim Extractor** (GPT-4o) — parses input into atomic, verifiable claims tagged with topic, specificity, verifiability score.
- **Research Agents** (GPT-4o, parallel, capped at 8 concurrent; >8 claims batched) — each runs a Query Decomposer (3–5 targeted queries) against News APIs (Tavily/Serper), Fact-Check DBs (ClaimBuster/Snopes/PolitiFact API), Official Sources (WHO/IMF/World Bank/Gov), Academic (Semantic Scholar/PubMed/Crossref), and Wikipedia (context-only, not primary evidence) — then an Evidence Ranker scores by credibility/date/relevance/consensus.
- **Analyst Agent** (GPT-4o) — mandatory adversarial framing: "evidence for" and "evidence against" are both required output fields (explicit "no contradicting evidence found" string if empty, never a blank field). Fringe-vs-consensus detection; OUTDATED detection for stale-but-was-accurate claims.
- **Verdict Agent** (GPT-4o) — citation-enforced prompt ("every factual statement must be followed by [SOURCE_N]"); schema validator rejects uncited output before Citation Verifier runs. Supports the 7-category taxonomy (TRUE/FALSE/PARTIALLY TRUE/MISLEADING/UNVERIFIABLE/OUTDATED/SATIRE).
- **Citation Verifier Agent** (GPT-4o-mini) — fetches every cited URL, text-searches for the attributed claim, strips hallucinated citations, reduces confidence proportionally.
- **Response Formatter** (GPT-4o-mini) — web/JSON output only (DM formatting deferred to Phase 3).
- **Source Credibility DB** (Postgres) — seed the 8-tier weighting table (Primary Gov/IGO 0.95 → Social Media 0.10).
- **Offline eval suite v1** — 500 pre-verified fact-check cases, run weekly, alert if pass rate < 82%.

**Constraints:**
- No real production traffic or real Reels — validated against curated test claims and a held-out benchmark set only.
- LLM spend is tracked informally; no automated cost gating yet.

**Trade-offs:**
- Isolating verdict quality from the harder Instagram/video integration reduces the risk of conflating "the AI reasoning is wrong" bugs with "the plumbing is broken" bugs — but it delays any real user or product-market signal until Phase 3.

**Exit Criteria:** ≥82% pass rate on the 500-case eval suite sustained across two consecutive weekly runs; median cost/verdict tracked and reported (no hard gate yet).

---

### **Phase 2 — Content Ingestion Pipeline (Video/Audio/OCR, Web Upload/URL)**

**Goal:** Turn an arbitrary Reel (via web upload or pasted URL) into the same structured content object the Phase 1 engine already consumes, with zero changes required to the verification engine.

**Non-Goals:** Instagram DM ingestion specifically (still web/URL-based), profiling layer, full 5-level degradation hierarchy (partial only).

**Engineering Requirements:**
- **URL Resolver** (normalizes platform URLs) → **Media Downloader** (3-strategy cascade: Meta API `media_url` → headless browser extraction → Apify Reel Scraper) → **Media Router** (Video / Image / Text-post).
- **Video Path:** ffmpeg (extract audio) → Whisper (transcript) → Frame Sampler (1 fps); GPT-4o Vision in parallel for frame description + on-screen-text OCR.
- **Caption Path:** Caption Extractor (from Meta API/scraper) → Hashtag/Link Extractor (topic context signals).
- **Structured Content Object** schema finalized: `{ transcript, visual_context, caption, source_url, topics[], language }`.
- **Orchestrator Agent** introduced — coordinates extraction and hands off to the Phase 1 verification engine.
- **S3** storage: video files (temporary, deleted after 24h for content-TOS compliance), transcripts (90-day retention for audit).
- Partial failure handling: video download fails → cascade + caption-only fallback; Whisper low confidence (<0.6) → flag + GPT-4o-vision-only fallback.

**Constraints:**
- The content router **must** emit an identical structured object regardless of which path (video/image/text-post) was taken — this is a hard interface contract with Phase 1.
- Scraper-based download strategies are inherently fragile to unannounced platform changes.

**Trade-offs:**
- Building extraction against generic URLs before the Instagram webhook integration risks some rework once the real Meta payload shape is finalized in Phase 3, but decouples "can we reliably parse arbitrary Reel content" from Meta's API review timeline, which the team does not control.

**Exit Criteria:** ≥95% successful structured-object generation across a test corpus spanning music-only reels, static-text reels, and caption-only submissions.

---

### **Phase 3 — Instagram DM Channel (Public Launch)**

**Goal:** Ship the actual product experience: forward a Reel via IG DM, get a verdict back in DM within target latency.

**Non-Goals:** Media-diet profiling/dashboard, semantic claim deduplication, full observability/alerting stack, 500k/day scale (targeting the 10k/day launch figure only).

**Engineering Requirements:**
- **DM Listener** (Meta Webhook) → **Job Queue** (Redis/SQS; priority tiers; per-user rate limit ≤5 requests/hour).
- **Instagram DM Reply** (Meta Graph API); **Response Formatter** DM mode — verdict label, one-paragraph plain-English explanation, confidence %, 2–3 source links, link to full web report, fit for a mobile screen (~≤280 chars core message).
- **Meta App Review** completed (Instagram Business Account + messaging permissions) — required before any production DM traffic.
- 24-hour messaging window handled via the natural "user DMs first" flow.
- Basic circuit breaker (single-tier: pause a tool after N consecutive failures) — full 5-level hierarchy deferred to Phase 4.
- P95 <120s end-to-end (DM receipt → DM reply) enforced and monitored.

**Constraints:**
- Meta App Review lead time is an external dependency outside engineering's control and can independently block launch.
- Instagram policy changes can break video-download strategies at any time — this is an ongoing operational risk, not a one-time launch risk.

**Trade-offs:**
- Shipping without semantic dedup means a viral claim gets independently re-researched for every submitting user — acceptable at 10k/day launch volume, but a known cost/latency risk if adoption outpaces the Phase 4 hardening timeline.

**Exit Criteria:** Public launch sustained at target volume with P95 <120s and no manual intervention required for >72 consecutive hours.

---

### **Phase 4 — Reliability, Deduplication & Scale Hardening**

**Goal:** Make the system economically sustainable and resilient enough to absorb viral spikes and scale toward the 500k/day roadmap figure.

**Non-Goals:** Media-diet/profiling features remain out of scope; web dashboard richness beyond the Phase 1 JSON format.

**Engineering Requirements:**
- **Claim deduplication:** new claims embedded and searched against the vector store (Pgvector/Qdrant); a semantically identical claim with a verdict from the last 7 days returns the cached verdict immediately.
- **Full 5-level degradation hierarchy:** (1) immediate retry, 2s wait → (2) exponential backoff 5s→15s → (3) tool swap (e.g., Tavily→Serper→Bing) → (4) degraded mode (fewer sources, shorter browse depth, cache-only) → (5) graceful UNVERIFIABLE with explicit failure explanation.
- **Circuit breaker:** open after 3 consecutive tool failures; 60s half-open probe; close on success.
- **Verdict cache** (Redis, URL hash → verdict, 24h TTL).
- Full 7-mode failure-handling table implemented end-to-end (video download, Whisper confidence, search API down, source 403/paywalled, citation inaccessible, DM delivery failure, LLM timeout).
- Full observability: OTEL tags on every agent span → Langfuse trace trees; Prometheus/Grafana dashboards against the 7-metric target/alert table (latency P95, queue depth, search success rate, UNVERIFIABLE rate, cost/check, citation fail rate, DM delivery rate).

**Constraints:**
- Dedup correctness depends entirely on embedding quality — semantically-identical-but-differently-worded claims must cluster correctly, or (worse) a near-but-not-identical claim could incorrectly be served a stale cached verdict.

**Trade-offs:**
- Prioritizing infrastructure reliability over new user-facing features (delays profiling/dashboard by one phase) — justified because an un-hardened system hitting a real viral spike risks both a runaway LLM bill and, more seriously, a wrong/stale verdict reaching a large audience.

**Exit Criteria:** Cost/verdict sustained <$0.15 under simulated viral load (1,000 duplicate submissions of one claim); all 7 metrics green for 2 consecutive weeks.

---

### **Phase 5 — Media Diet Intelligence & Web Dashboard**

**Goal:** Deliver the long-term retention layer: a personal information-health report and a rich web experience.

**Non-Goals:** No changes to core verdict logic — this phase is additive analytics on top of existing verdict data.

**Engineering Requirements:**
- **Profiling Agent** (GPT-4o-mini, async, triggered by `verdict_completed` event, fully decoupled from the main pipeline via event bus — zero added latency to fact-check delivery).
- Consumption analytics: topic distribution, rolling 30-day accuracy score, source diversity score, temporal accuracy trend.
- Echo-chamber indicators: political lean score (AllSides + Ad Fontes Media bias data, presented as a spectrum), claim repetition rate, counter-exposure index (% of consumed content presenting the opposing view — the canonical echo-chamber signal).
- Per-user submission vector clustering (second Pgvector/Qdrant collection).
- Threshold alerts: accuracy_score < 40% → "information diet alert" email/DM; echo_score > 0.85 → "Filter Bubble" dashboard insight card.
- Web dashboard: claim-by-claim breakdown, expandable sources panel with excerpts/credibility scores, for/against evidence-weight visualization, shareable PNG verdict cards.

**Constraints:**
- Political lean scoring must be presented "as a spectrum, not a judgment," per the underlying design intent — this is a UX/framing constraint, not just a technical one.

**Trade-offs:**
- Sequenced last because it's architecturally safe to omit through Phases 1–4 (event-bus decoupling means it never touches the core value loop) — but that also means users get zero retention-layer value until this phase ships.

**Exit Criteria:** Dashboard live for 100% of users with ≥5 historical submissions; profiling adds zero measurable latency to the DM verdict path.

---

### **Phase 6 — Scale to 500,000/day**

**Goal:** Close out the roadmap NFR.

**Engineering Requirements:** Load testing to 500k/day; auto-scaling validation on queue-depth triggers; re-validation of the 8-parallel-Research-Agent cap under sustained high concurrency; cost model re-validation at scale.

**Exit Criteria:** Sustained 500k/day in staged load test with all Phase 4 metrics still green.

---

## 8. Cross-Cutting Constraints

- Meta Messaging API 24-hour reply window and App Review gate the entire Instagram channel.
- Web search APIs, the LLM API, and Instagram's Messaging API (200 DM/hour) all have independent rate limits that can cascade into system-wide failure if not isolated per-tool.
- Video files are never retained beyond 24 hours (content TOS compliance); transcripts retained 90 days for audit only.

## 9. Risks & Open Questions

1. The source article's own verdict taxonomy is ambiguous: body text references a **DISPUTED** verdict for 50/50-split evidence, but the canonical taxonomy only lists 7 categories. Needs a decision before Phase 1 ships.
2. Diagram 6 (orchestration flow) shows an "Evidence Collector" merge step between Research Agents and the Analyst Agent that isn't represented in the 9-agent roster — needs clarification on whether it's a 10th agent or a non-LLM merge utility.
3. The source material references "four overlapping mechanisms" against hallucination but only enumerates three explicitly.

---

## 10. Appendix

**Verdict Taxonomy (7 categories, per source diagram):** ✅ TRUE · ❌ FALSE · ⚠️ PARTIALLY TRUE · 🔶 MISLEADING · ❓ UNVERIFIABLE · 📅 OUTDATED · 🎭 SATIRE

**Agent Roster:** Orchestrator (GPT-4o) · Content Extractor (GPT-4o-mini) · Claim Extractor (GPT-4o) · Research Agents ×N (GPT-4o) · Analyst Agent (GPT-4o) · Verdict Agent (GPT-4o) · Citation Verifier (GPT-4o-mini) · Response Formatter (GPT-4o-mini) · Profiling Agent (GPT-4o-mini, async)

**Storage:** PostgreSQL (relational) · Pgvector/Qdrant (embeddings) · Redis (queue/cache) · S3 (binaries)
