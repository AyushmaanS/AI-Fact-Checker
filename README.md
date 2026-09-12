# AI Fact-Checker

An AI agent pipeline that fact-checks a claim and returns a verdict — TRUE, FALSE,
PARTIALLY_TRUE, MISLEADING, UNVERIFIABLE, OUTDATED, or SATIRE — grounded in
independently-verified sources. Every factual statement in a verdict must cite a
source; uncited output is rejected before it reaches the response.

Currently Phase 1 (raw text/caption in, no Instagram or video ingestion yet).

Planning docs live in [`docs/`](docs/): the PRD, functional spec, engineering
requirements, and sprint plan. Each sprint's Claude Code prompt lives in
`docs/phase1-2-sprint-plan.md`.

## Setup

1. Create and activate a virtual environment:

   ```bash
   python -m venv venv
   # Windows (PowerShell): venv\Scripts\Activate.ps1
   # Windows (git-bash):    source venv/Scripts/activate
   # macOS/Linux:           source venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in your keys:

   ```bash
   cp .env.example .env
   ```

4. Run the dev server:

   ```bash
   uvicorn app.main:app --reload
   ```

5. Check it's alive: `http://127.0.0.1:8000/health`, or
   `http://127.0.0.1:8000/docs` for the interactive Swagger UI.

## Database (Supabase)

1. Create a free project at [supabase.com](https://supabase.com).
2. Open **SQL Editor** in the Supabase dashboard, paste in the contents of
   [`app/db/schema.sql`](app/db/schema.sql), and run it. This creates the
   `submissions`, `claims`, `verdicts`, and `source_credibility` tables and
   seeds the credibility-weight table.
3. In **Project Settings → API**, copy the **Project URL** and the
   **anon/public API key**.
4. Put them in `.env` as `SUPABASE_URL` and `SUPABASE_KEY`.

`tests/test_db.py` auto-skips until these are set, so the rest of the suite
runs fine without a Supabase project — the DB tests activate the moment
`.env` has real values.

## Tests

```bash
pytest
```
