import logging
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10.0

# Query params that only carry tracking/attribution info, never anything that
# changes what the URL points to - safe to strip unconditionally.
TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAMS = {"igsh", "igshid", "fbclid", "gclid", "ref", "ref_src", "si"}

PLATFORM_DOMAINS = {
    "instagram.com": "instagram",
    "instagr.am": "instagram",
}


class ResolvedURL(BaseModel):
    canonical_url: str
    platform: str  # "instagram" or "unknown"


def _strip_tracking_params(url: str) -> str:
    parsed = urlparse(url)
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in TRACKING_PARAMS and not key.startswith(TRACKING_PARAM_PREFIXES)
    ]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def _detect_platform(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return PLATFORM_DOMAINS.get(domain, "unknown")


async def _follow_redirects(url: str) -> str:
    """Best-effort: resolves a genuine shortlink to its final destination. A
    blocked or failed request just keeps the original URL rather than raising -
    the same "can't check, not a failure" stance citation_verifier takes on an
    unfetchable page. Confirmed live: unlike Wikipedia/Reuters (which reject
    citation_verifier's fetches outright), an unauthenticated HEAD to Instagram
    doesn't get blocked - it 200s even for a made-up reel id, since the real
    content loads client-side. Still worth a graceful fallback for genuinely
    unreachable shortlink targets (DNS failure, timeout, real 4xx/5xx)."""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            response = await client.head(url)
            return str(response.url)
    except httpx.HTTPError as exc:
        logger.info("url_resolver: could not follow redirects for %s (%s)", url, exc)
        return url


async def resolve_url(raw_url: str) -> ResolvedURL:
    """Strips tracking params and follows redirects to a canonical URL, then
    detects the platform. If the URL's domain is already a recognized platform,
    no network call is made at all - just param-stripping - both because it's
    the common case (a pasted instagram.com link) and because a live fetch
    wouldn't tell us anything useful there anyway (see _follow_redirects). A
    domain we don't recognize is treated as a possible shortlink and its
    redirect target is resolved before re-checking the platform."""
    raw_url = raw_url.strip()
    resolved = raw_url if _detect_platform(raw_url) != "unknown" else await _follow_redirects(raw_url)
    canonical = _strip_tracking_params(resolved)
    return ResolvedURL(canonical_url=canonical, platform=_detect_platform(canonical))
