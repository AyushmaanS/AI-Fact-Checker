from supabase import Client, create_client

from app.config import SUPABASE_KEY, SUPABASE_URL

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def insert_submission(raw_input: str, input_type: str) -> dict:
    result = get_client().table("submissions").insert(
        {"raw_input": raw_input, "input_type": input_type}
    ).execute()
    return result.data[0]


def get_submission(submission_id: str) -> dict | None:
    result = get_client().table("submissions").select("*").eq("id", submission_id).execute()
    return result.data[0] if result.data else None


def insert_claim(
    submission_id: str,
    text: str,
    topic: str,
    specificity: str,
    verifiability_score: float,
) -> dict:
    result = get_client().table("claims").insert(
        {
            "submission_id": submission_id,
            "text": text,
            "topic": topic,
            "specificity": specificity,
            "verifiability_score": verifiability_score,
        }
    ).execute()
    return result.data[0]


def get_claim(claim_id: str) -> dict | None:
    result = get_client().table("claims").select("*").eq("id", claim_id).execute()
    return result.data[0] if result.data else None


def insert_verdict(
    claim_id: str,
    label: str,
    rationale: str,
    confidence_score: float,
    citations: list[str],
) -> dict:
    result = get_client().table("verdicts").insert(
        {
            "claim_id": claim_id,
            "label": label,
            "rationale": rationale,
            "confidence_score": confidence_score,
            "citations": citations,
        }
    ).execute()
    return result.data[0]


def get_verdict(verdict_id: str) -> dict | None:
    result = get_client().table("verdicts").select("*").eq("id", verdict_id).execute()
    return result.data[0] if result.data else None
