from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.ingestion.photo_post_downloader import (
    PhotoPostResult,
    _pick_image_url,
    download_photo_post,
    extract_shortcode,
)


def test_extract_shortcode_from_post_url():
    assert extract_shortcode("https://www.instagram.com/p/DdjgEyRt9ua/") == "DdjgEyRt9ua"


def test_extract_shortcode_from_reel_url():
    assert extract_shortcode("https://www.instagram.com/reel/Ddg4_CVBQX9/") == "Ddg4_CVBQX9"


def test_extract_shortcode_returns_none_for_non_instagram_url():
    assert extract_shortcode("https://example.com/not-instagram/") is None


def _make_single_image_post(caption="A caption #news", hashtags=None):
    post = MagicMock()
    post.typename = "GraphImage"
    post.url = "https://scontent.cdninstagram.com/image123.jpg"
    post.caption = caption
    post.caption_hashtags = hashtags or ["news"]
    return post


def _make_carousel_post(image_urls, caption="Carousel caption"):
    post = MagicMock()
    post.typename = "GraphSidecar"
    post.caption = caption
    post.caption_hashtags = []
    post.get_sidecar_nodes.return_value = [
        MagicMock(is_video=False, display_url=url, video_url=None) for url in image_urls
    ]
    return post


def test_pick_image_url_uses_post_url_for_single_image():
    post = _make_single_image_post()
    assert _pick_image_url(post) == "https://scontent.cdninstagram.com/image123.jpg"


def test_pick_image_url_selects_first_image_of_carousel(caplog):
    post = _make_carousel_post(["https://cdn/first.jpg", "https://cdn/second.jpg", "https://cdn/third.jpg"])

    with caplog.at_level("INFO"):
        picked = _pick_image_url(post)

    assert picked == "https://cdn/first.jpg"
    assert "carousel with 3 images detected, using first image only" in caplog.text.lower()


@patch("app.ingestion.photo_post_downloader._fetch_post_sync")
async def test_download_photo_post_success(mock_fetch, monkeypatch):
    mock_fetch.return_value = _make_single_image_post(caption="Real caption #breaking", hashtags=["breaking"])

    async def fake_get(self, url):
        return httpx.Response(200, content=b"fake-image-bytes", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await download_photo_post("https://www.instagram.com/p/abc123/")

    assert isinstance(result, PhotoPostResult)
    assert result.caption == "Real caption #breaking"
    assert result.hashtags == ["breaking"]
    assert Path(result.image_path).read_bytes() == b"fake-image-bytes"
    assert Path(result.image_path).suffix == ".jpg"
    mock_fetch.assert_called_once_with("abc123")


async def test_download_photo_post_raises_for_unparseable_url():
    with pytest.raises(ValueError):
        await download_photo_post("https://example.com/not-instagram/")


@patch("app.ingestion.photo_post_downloader._fetch_post_sync")
async def test_download_photo_post_propagates_fetch_failure(mock_fetch):
    mock_fetch.side_effect = RuntimeError("instaloader: 401 Unauthorized")

    with pytest.raises(RuntimeError):
        await download_photo_post("https://www.instagram.com/p/abc123/")
