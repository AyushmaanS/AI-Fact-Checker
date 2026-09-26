# AI Fact-Checker

An AI agent pipeline that fact-checks a claim and returns a verdict — TRUE, FALSE,
PARTIALLY_TRUE, MISLEADING, UNVERIFIABLE, OUTDATED, or SATIRE — grounded in
independently-verified sources. Every factual statement in a verdict must cite a
source; uncited output is rejected before it reaches the response.

Takes a claim as plain text, an Instagram URL (Reels, videos, and photo posts),
or a directly uploaded video/image. Both Phase 1 (the research/verdict pipeline)
and Phase 2 (Instagram/media ingestion) are done — see
[`docs/current-state.md`](docs/current-state.md) for the full current picture.
A Streamlit UI (see "Running the frontend" below) is the quickest way to try it.

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
2. Open **SQL Editor** in the Supabase dashboard, paste in the entire contents
   of [`app/db/schema.sql`](app/db/schema.sql), and run it in one go. It's a
   single self-contained script (safe to run once against a fresh project):
   creates the `submissions`, `claims`, `verdicts`, and `source_credibility`
   tables, seeds the 67-domain credibility-weight table (international +
   India-context sources), creates the private `media` Storage bucket used for
   temporarily-held video files, and adds the RLS policies that bucket needs
   under the `anon` key.
3. In **Project Settings → API**, copy the **Project URL** and the
   **anon/public API key**.
4. Put them in `.env` as `SUPABASE_URL` and `SUPABASE_KEY`.

`tests/test_db.py` auto-skips until these are set, so the rest of the suite
runs fine without a Supabase project — the DB tests activate the moment
`.env` has real values.

## Running the frontend

A simple Streamlit UI lives in [`frontend/`](frontend/). It calls the backend over
HTTP, so it needs the API server running in a separate terminal:

```bash
# Terminal 1
uvicorn app.main:app --reload

# Terminal 2
streamlit run frontend/streamlit_app.py
```

Streamlit opens at `http://localhost:8501`. It talks to the backend at the
`API_BASE_URL` env var (defaults to `http://localhost:8000`, already set in
`.env.example`).

## Tests

```bash
pytest
```
