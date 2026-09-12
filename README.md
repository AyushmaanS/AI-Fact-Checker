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

## Tests

```bash
pytest
```
