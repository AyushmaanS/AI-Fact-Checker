from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.ingestion.url_resolver import _detect_platform, _strip_tracking_params, resolve_url


def test_strip_tracking_params_removes_igsh():
    messy = "https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=MWQ1ZjNjMTBjNw=="
    assert _strip_tracking_params(messy) == "https://www.instagram.com/reel/C1a2B3c4D5e/"


def test_strip_tracking_params_removes_multiple_tracking_params_together():
    messy = "https://instagram.com/p/ABC123xyz/?utm_source=ig_web_copy_link&igshid=abc123"
    assert _strip_tracking_params(messy) == "https://instagram.com/p/ABC123xyz/"


def test_strip_tracking_params_leaves_clean_url_unchanged():
    clean = "https://www.instagram.com/reel/C1a2B3c4D5e/"
    assert _strip_tracking_params(clean) == clean


def test_strip_tracking_params_keeps_non_tracking_query_params():
    # Only the known tracking keys are stripped - anything else is left alone,
    # since it might actually matter (e.g. a real content id).
    url = "https://example.com/post?id=42&igsh=abc123"
    assert _strip_tracking_params(url) == "https://example.com/post?id=42"


def test_detect_platform_instagram_with_and_without_www():
    assert _detect_platform("https://www.instagram.com/reel/xyz/") == "instagram"
    assert _detect_platform("https://instagram.com/reel/xyz/") == "instagram"
    assert _detect_platform("https://instagr.am/reel/xyz/") == "instagram"


def test_detect_platform_unknown_for_other_domains():
    assert _detect_platform("https://example.com/foo") == "unknown"


async def test_resolve_url_instagram_link_never_hits_network():
    # Known-platform domains skip the redirect-follow entirely (see module
    # docstring) - for speed, and because a live fetch wouldn't tell us
    # anything useful for a domain we already recognize.
    messy = "https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=MWQ1ZjNjMTBjNw=="
    with patch("app.ingestion.url_resolver.httpx.AsyncClient") as mock_client_cls:
        result = await resolve_url(messy)
        mock_client_cls.assert_not_called()

    assert result.canonical_url == "https://www.instagram.com/reel/C1a2B3c4D5e/"
    assert result.platform == "instagram"


async def test_resolve_url_strips_leading_and_trailing_whitespace():
    result = await resolve_url("  https://www.instagram.com/p/ABC123/?igsh=xyz  ")
    assert result.canonical_url == "https://www.instagram.com/p/ABC123/"


async def test_resolve_url_follows_redirect_for_unrecognized_domain():
    mock_response = MagicMock()
    mock_response.url = "https://www.instagram.com/reel/landed-here/?igsh=abc"
    mock_client = MagicMock()
    mock_client.head = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.ingestion.url_resolver.httpx.AsyncClient", return_value=mock_client):
        result = await resolve_url("https://bit.ly/some-shortlink")

    mock_client.head.assert_called_once()
    assert result.canonical_url == "https://www.instagram.com/reel/landed-here/"
    assert result.platform == "instagram"


async def test_resolve_url_falls_back_to_original_on_failed_redirect_follow():
    mock_client = MagicMock()
    mock_client.head = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.ingestion.url_resolver.httpx.AsyncClient", return_value=mock_client):
        result = await resolve_url("https://bit.ly/dead-link?utm_source=x")

    # Couldn't follow it, so it falls back to the original URL - tracking params
    # still get stripped from whatever URL we end up with.
    assert result.canonical_url == "https://bit.ly/dead-link"
    assert result.platform == "unknown"
