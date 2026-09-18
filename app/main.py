from fastapi import FastAPI

from app import config  # noqa: F401  (import triggers .env loading on startup)
from app.routes.verify import router as verify_router

app = FastAPI(title="AI Fact-Checker", version="0.1.0")
app.include_router(verify_router)


@app.get("/health")
def health():
    return {"status": "ok"}
