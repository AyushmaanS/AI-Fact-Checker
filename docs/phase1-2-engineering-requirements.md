# Engineering Requirements Document — Phase 1 & Phase 2
**Derived from:** `prd-v1-draft.md` + `phase1-2-functional-spec.md` · **Status:** Ready for build

This is the technical implementation companion to the functional spec. It makes the concrete tech-stack calls the PRD leaves open, so building can start immediately without a separate research pass.

---

## 1. Tech Stack (opinionated defaults)

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.11+** | Best LLM/agent ecosystem; easiest for AI-assisted development. |
| Web framework | **FastAPI** | Pairs natively with Pydantic (which we need anyway for schema validation); auto-generates a Swagger UI at `/docs` for manual testing without writing a frontend. |
| Schema/validation | **Pydantic v2** | Doubles as the "schema validator" the article requires for citation-enforcement. |
| Concurrency | **Python `asyncio`** | Native, no extra framework needed to run Research Agents in parallel. |
| LLM provider | **OpenAI models** (`gpt-4o`, `gpt-4o-mini`) via **FastRouter**'s OpenAI-SDK-compatible gateway (`api.fastrouter.ai`), same `chat.completions.parse()` structured-output calls, just routed through provider-prefixed dated model IDs (`openai/gpt-4o-mini-2024-07-18`, `openai/gpt-4o-2024-05-13`) instead of `api.openai.com` directly. Whisper (Phase 2, Sprint 15) still needs confirming against FastRouter's audio-transcription support — may fall back to calling OpenAI directly for that one endpoint. |
| Search | **Tavily API only for Phase 1–2** | *Scoping decision:* the source article lists 5 source types (News APIs, Fact-Check DBs, Official Sources, Academic, Wikipedia) as separate integrations. Wiring all 5 individually is a lot of setup for one person. Tavily's general web search already surfaces most of these source types, and the credibility-weighting table does the work of favoring high-quality domains after the fact. **Defer the additional dedicated APIs (ClaimBuster, Semantic Scholar, etc.) to a later reliability phase** — flagged here explicitly, not silently dropped. |
| Video download | **`yt-dlp`** (open-source) | Realistic, well-maintained substitute for the article's "headless browser extraction" strategy; free, no third-party service account needed. *(Apify Reel Scraper, the article's strategy 3, is a paid fallback — worth adding later if `yt-dlp` proves unreliable, not required for Phase 1–2.)* |
| Media processing | **`ffmpeg`** (system binary) + **OpenCV or ffmpeg frame extraction** | Audio extraction and 1fps frame sampling exactly as specified. |
| Database | **Supabase** (managed Postgres + Storage) | One account gives you Postgres (matches the PRD's storage choice) **and** S3-compatible object storage (needed in Phase 2 for video/transcripts) — avoids juggling two cloud accounts as a solo builder. Generous free tier. |
| Testing | **pytest** | Standard, well-supported by Claude-generated code. |

---

## 2. Repo Structure

```
fact-checker/
├── app/
│   ├── main.py                  # FastAPI app entrypoint
│   ├── config.py                # env/config loading
│   ├── pipeline.py              # orchestrates the full agent pipeline
│   ├── models/
│   │   └── schemas.py           # all Pydantic models (Part A.2 + B.2 of the spec)
│   ├── agents/
│   │   ├── intent_classifier.py
│   │   ├── claim_extractor.py
│   │   ├── research_agent.py
│   │   ├── evidence_ranker.py
│   │   ├── analyst_agent.py
│   │   ├── verdict_agent.py
│   │   ├── citation_verifier.py
│   │   └── response_formatter.py
│   ├── ingestion/                # Phase 2 only
│   │   ├── url_resolver.py
│   │   ├── media_downloader.py
│   │   ├── media_router.py
│   │   ├── video_path.py
│   │   └── caption_path.py
│   ├── db/
│   │   ├── client.py             # Supabase client
│   │   └── schema.sql            # DDL, see §3
│   └── routes/
│       └── verify.py             # /verify, /verify/upload, /verify/url, /health
├── eval/
│   ├── cases/                    # hand-written test claims (grows toward the PRD's 500)
│   └── run_eval.py
├── tests/                        # mirrors app/ structure
├── docs/                         # drop prd-v1-draft.md, this ERD, the spec, and the sprint plan here
├── .env.example
├── requirements.txt
└── README.md
```

Keep all four planning docs in `docs/` inside the repo — Claude Code can then read them directly for context each sprint instead of having everything re-pasted into every prompt.

---

## 3. Database Schema (Postgres, via Supabase)

```sql
create table submissions (
    id uuid primary key default gen_random_uuid(),
    raw_input text,
    input_type text check (input_type in ('text','upload','url')),
    created_at timestamptz default now()
);

create table claims (
    id uuid primary key default gen_random_uuid(),
    submission_id uuid references submissions(id),
    text text not null,
    topic text,
    specificity text,
    verifiability_score float
);

create table verdicts (
    id uuid primary key default gen_random_uuid(),
    claim_id uuid references claims(id),
    label text check (label in ('TRUE','FALSE','PARTIALLY_TRUE','MISLEADING','UNVERIFIABLE','OUTDATED','SATIRE')),
    rationale text,
    confidence_score float,
    citations jsonb,
    created_at timestamptz default now()
);

create table source_credibility (
    domain_pattern text primary key,
    category text,
    weight float check (weight >= 0 and weight <= 1)
);
-- seed with the 8 tiers from the spec/PRD: Primary Gov/IGO 0.95 ... Social Media 0.10
```

No vector DB, Redis, or S3-for-video table needed until Phase 2 (Storage bucket, not a SQL table) and Phase 4 (vector DB), respectively — don't provision them early.

---

## 4. Non-Functional Requirements (technical translation)

**Latency budget** (target, not hard-gated until Phase 3's P95 <120s NFR — but design toward it now):

| Stage | Budget |
|---|---|
| Content Intent Classification | < 3s |
| Claim Extraction | < 5s |
| Research per claim (parallel, so this is the ceiling not the sum) | < 40s |
| Analyst + Verdict generation | < 15s |
| Citation Verification | < 10s |
| **Total target** | **~75–90s** |

**Error handling:** every agent function wraps its LLM/API calls in try/except; failures propagate a structured error (never a raw stack trace to the API response). No retry logic beyond a single immediate retry is required in Phase 1–2 — the full 5-level degradation hierarchy is explicitly Phase 4 scope.

**Cost tracking:** log token usage and estimated cost on every LLM call (informal — PRD v1 does not require automated gating until Phase 4, only "tracked informally"). A simple `cost_log` table or even structured stdout logging is sufficient for now.

**Testing:** unit tests per agent function using mocked LLM/search responses; a small starter eval set (aim for 20–30 hand-picked cases spanning most of the 7 verdict labels) rather than the PRD's full 500-case suite — that set is built incrementally, not on day one.

**Logging/observability:** structured logging (Python `logging` module, JSON-formatted) is sufficient for Phase 1–2. Full Langfuse/Prometheus/Grafana instrumentation is Phase 4 scope — don't build it early.

**Security:** all API keys (`OPENAI_API_KEY`, `TAVILY_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`) live in `.env`, never committed; `.env` is gitignored from Sprint 0.

**Deployment:** local development first. Once Phase 2 is stable, a low-cost host (Render, Railway, or Fly.io) is a reasonable stretch target — not required for Phase 1–2 completion.

---

## 5. Explicit Simplifications vs. the Full PRD (tracked, not hidden)

| PRD says | Phase 1–2 build does instead | Revisit when |
|---|---|---|
| 5 separate research source integrations | Tavily only | Reliability hardening phase |
| Apify Reel Scraper as fallback | `yt-dlp` only | If `yt-dlp` reliability proves insufficient |
| 500-case offline eval suite | 20–30 case starter set | Ongoing, grows every sprint |
| Automated cost gating | Informal logging only | Phase 4 (per PRD v1) |
