import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional

import yt_dlp
from pydantic import BaseModel

from app.ingestion.photo_post_downloader import download_photo_post

logger = logging.getLogger(__name__)

DOWNLOAD_FAILED_TEXT = "Could not download this content."
# Set when yt-dlp reports no video AND the instaloader fallback below also
# couldn't get the photo - distinct from DOWNLOAD_FAILED_TEXT so verify.py can
# give a more specific message than "private/deleted/rate-limited", which
# isn't what happened here.
PHOTO_POST_FALLBACK_FAILED_ERROR = "photo_post_fallback_failed"
# yt-dlp's own error text when a post has no video at all (a photo post) -
# it parses the caption internally but discards it before returning, once it
# sees there are no video formats. Two different messages depending on the
# code path: "There is no video in this post" (instagram.py's own
# raise_no_formats, for a single non-carousel post) or the generic "No video
# formats found!" (YoutubeDL's own core-level check, which fires instead for
# a carousel - the Instagram-specific check only runs when the post isn't a
# playlist/carousel; confirmed live against a real 5-item mixed photo/video
# carousel). Unlike a real rate-limit/private-post failure, retrying the
# metadata-only attempt is pointless for either - the instaloader fallback is
# tried instead.
_NO_VIDEO_MARKERS = ("no video in this post", "no video formats found")

_YDL_OPTS_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    # A single pre-muxed file, not bestvideo+bestaudio - avoids needing ffmpeg to
    # merge streams, which is out of scope until Sprint 15.
    "format": "best[ext=mp4]/best",
}


class DownloadResult(BaseModel):
    success: bool
    file_path: Optional[str] = None
    caption: Optional[str] = None
    error: Optional[str] = None


def _extract_sync(url: str, download: bool, temp_dir: str) -> dict:
    opts = {**_YDL_OPTS_BASE, "outtmpl": f"{temp_dir}/%(id)s.%(ext)s"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=download)


def _find_downloaded_file(temp_dir: str) -> Optional[str]:
    files = [p for p in Path(temp_dir).iterdir() if p.is_file()]
    return str(files[0]) if files else None


def _is_no_video_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _NO_VIDEO_MARKERS)


async def _try_photo_post_fallback(url: str) -> DownloadResult:
    try:
        result = await download_photo_post(url)
    except Exception as exc:
        logger.info("media_downloader: photo-post fallback also failed for %s (%s)", url, exc)
        return DownloadResult(success=False, error=PHOTO_POST_FALLBACK_FAILED_ERROR)
    return DownloadResult(success=True, file_path=result.image_path, caption=result.caption)


async def download_media(url: str) -> DownloadResult:
    """Downloads a video via yt-dlp into a fresh temp directory. Never raises -
    a private/deleted/rate-limited post is a normal, expected outcome here, not
    an exceptional one (spec B.5: "graceful caption-only fallback or a clear
    user-facing error, never a raw stack trace"). If the full download fails
    with yt-dlp's specific "no video in this post" error, the post has no video
    at all (a photo post) - falls straight to the instaloader-based photo-post
    fallback rather than yt-dlp's own metadata-only retry, which would just
    fail the same way again. Any other failure makes one more attempt at
    metadata only (no download) - some failure modes (e.g. a rate limit that
    only affects the media CDN) still leave the post's own caption/description
    reachable even when the video itself isn't, and that's exactly what the
    caption-only fallback needs."""
    temp_dir = tempfile.mkdtemp(prefix="ingest_")

    try:
        info = await asyncio.to_thread(_extract_sync, url, True, temp_dir)
        file_path = _find_downloaded_file(temp_dir)
        if file_path is None:
            raise RuntimeError("yt-dlp reported success but wrote no file")
        return DownloadResult(success=True, file_path=file_path, caption=info.get("description"))
    except Exception as exc:
        logger.info("media_downloader: full download failed for %s (%s)", url, exc)
        if _is_no_video_error(exc):
            return await _try_photo_post_fallback(url)

    try:
        info = await asyncio.to_thread(_extract_sync, url, False, temp_dir)
        return DownloadResult(success=False, caption=info.get("description"), error=DOWNLOAD_FAILED_TEXT)
    except Exception as exc:
        logger.info("media_downloader: metadata-only fetch also failed for %s (%s)", url, exc)
        return DownloadResult(success=False, error=DOWNLOAD_FAILED_TEXT)
