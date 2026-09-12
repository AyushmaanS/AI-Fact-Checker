from fastapi import FastAPI

from app import config  # noqa: F401  (import triggers .env loading on startup)

app = FastAPI(title="AI Fact-Checker", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}
