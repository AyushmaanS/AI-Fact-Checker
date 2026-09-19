from openai import AsyncOpenAI, OpenAI

from app.config import FASTROUTER_API_KEY

# FastRouter (https://fastrouter.ai) is an OpenAI-SDK-compatible gateway - same
# client, same chat.completions.parse() structured-output API, just routed
# through their endpoint with provider-prefixed, dated model IDs.
BASE_URL = "https://api.fastrouter.ai/api/v1"

# NOTE: gpt-4o-2024-05-13 does NOT support response_format=json_schema (Structured
# Outputs shipped with the 2024-08-06 snapshot) - use a later dated snapshot here.
MODEL_GPT4O = "openai/gpt-4o-2024-11-20"
MODEL_GPT4O_MINI = "openai/gpt-4o-mini-2024-07-18"

# The SDK's own default read timeout is 600s (confirmed via the installed
# client's own .timeout) - fine for arbitrarily long streamed generations, but
# nowhere near appropriate for this product's ~90s end-to-end latency target.
# Traced two live test runs that each took ~610-625s (not a coincidence - 600s
# default + retry overhead) back to exactly this: an occasional FastRouter
# stall with no client-side guardrail, so the call just waited out the SDK
# default instead of failing fast. 60s is still generous relative to this
# codebase's own per-agent budgets (e.g. citation verification's own 10s
# per-fetch timeout) while bounding the worst case to something sane.
REQUEST_TIMEOUT = 60.0

_client: OpenAI | None = None
_async_client: AsyncOpenAI | None = None


def get_llm_client() -> OpenAI:
    global _client
    if _client is None:
        if not FASTROUTER_API_KEY:
            raise RuntimeError("FASTROUTER_API_KEY must be set in .env")
        _client = OpenAI(base_url=BASE_URL, api_key=FASTROUTER_API_KEY, timeout=REQUEST_TIMEOUT)
    return _client


def get_async_llm_client() -> AsyncOpenAI:
    global _async_client
    if _async_client is None:
        if not FASTROUTER_API_KEY:
            raise RuntimeError("FASTROUTER_API_KEY must be set in .env")
        _async_client = AsyncOpenAI(
            base_url=BASE_URL, api_key=FASTROUTER_API_KEY, timeout=REQUEST_TIMEOUT
        )
    return _async_client
