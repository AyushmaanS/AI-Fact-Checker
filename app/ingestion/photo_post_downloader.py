import asyncio
import logging
import re
import tempfile
from pathlib import Path
from typing import Optional

import httpx
import instaloader
from pydantic import BaseModel

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0

# Not reused from url_resolver.py - that module only strips tracking params
# and detects platform, it has no shortcode-parsing to adapt. Covers /p/,
# /reel/, /reels/, and /tv/ (legacy IGTV), all of which key by shortcode.
_SHORTCODE_PATTERN = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")


class PhotoPostResult(BaseModel):
    image_path: str
    caption: Optional[str] = None
    hashtags: list[str] = []


def extract_shortcode(url: str) -> Optional[str]:
    match = _SHORTCODE_PATTERN.search(url)
    return match.group(1) if match else None


def _fetch_post_sync(shortcode: str) -> instaloader.Post:
    context = instaloader.Instaloader(quiet=True).context
    return instaloader.Post.from_shortcode(context, shortcode)


def _pick_image_url(post: instaloader.Post) -> str:
    if post.typename == "GraphSidecar":
        nodes = list(post.get_sidecar_nodes())
        logger.info("photo_post_downloader: carousel with %d images detected, using first image only", len(nodes))
        return nodes[0].display_url
    return post.url


async def download_photo_post(url: str) -> PhotoPostResult:
    """Fallback for Instagram photo posts, which yt-dlp (a video-only tool)
    can't extract at all (see media_downloader._NO_VIDEO_MARKER). Fetches post
    data anonymously via instaloader (no login), then downloads just the
    single image's bytes directly - not instaloader's own download_post(),
    which writes extra sidecar/metadata files this app has no use for.

    Anonymous fetching is subject to the same kind of request throttling as
    the yt-dlp path - this is another extraction strategy that mostly works,
    not a guaranteed fix. An authenticated (logged-in) Instaloader session is
    a possible future lever if this proves too rate-limited - out of scope
    here.

    Raises on any failure (bad/missing shortcode, inaccessible post, request
    failure) rather than returning a sentinel - the caller
    (media_downloader.download_media) decides what that means for the
    user-facing message."""
    shortcode = extract_shortcode(url)
    if shortcode is None:
        raise ValueError(f"Could not find an Instagram shortcode in {url}")

    post = await asyncio.to_thread(_fetch_post_sync, shortcode)
    image_url = _pick_image_url(post)

    temp_dir = tempfile.mkdtemp(prefix="photo_")
    suffix = Path(image_url.split("?")[0]).suffix or ".jpg"
    image_path = str(Path(temp_dir) / f"{shortcode}{suffix}")

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(image_url)
        response.raise_for_status()
    Path(image_path).write_bytes(response.content)

    return PhotoPostResult(image_path=image_path, caption=post.caption, hashtags=list(post.caption_hashtags))
