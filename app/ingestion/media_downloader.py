import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional

import yt_dlp
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DOWNLOAD_FAILED_TEXT = "Could not download this content."
# Set instead of DOWNLOAD_FAILED_TEXT when yt-dlp's own error makes clear the
# post has no video at all (a photo post/carousel) rather than being private,
# deleted, or rate-limited - see instagram.py's raise_no_formats call, which
# discards the caption it already fetched instead of returning it.
NO_VIDEO_ERROR = "no_video_in_post"
_NO_VIDEO_MARKER = "no video in this post"

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
    return _NO_VIDEO_MARKER in str(exc).lower()


async def download_media(url: str) -> DownloadResult:
    """Downloads a video via yt-dlp into a fresh temp directory. Never raises -
    a private/deleted/rate-limited post is a normal, expected outcome here, not
    an exceptional one (spec B.5: "graceful caption-only fallback or a clear
    user-facing error, never a raw stack trace"). If the full download fails,
    makes one more attempt at metadata only (no download) - some failure modes
    (e.g. a rate limit that only affects the media CDN) still leave the post's
    own caption/description reachable even when the video itself isn't, and
    that's exactly what the caption-only fallback needs."""
    temp_dir = tempfile.mkdtemp(prefix="ingest_")
    error = DOWNLOAD_FAILED_TEXT

    try:
        info = await asyncio.to_thread(_extract_sync, url, True, temp_dir)
        file_path = _find_downloaded_file(temp_dir)
        if file_path is None:
            raise RuntimeError("yt-dlp reported success but wrote no file")
        return DownloadResult(success=True, file_path=file_path, caption=info.get("description"))
    except Exception as exc:
        logger.info("media_downloader: full download failed for %s (%s)", url, exc)
        if _is_no_video_error(exc):
            error = NO_VIDEO_ERROR

    try:
        info = await asyncio.to_thread(_extract_sync, url, False, temp_dir)
        return DownloadResult(success=False, caption=info.get("description"), error=error)
    except Exception as exc:
        logger.info("media_downloader: metadata-only fetch also failed for %s (%s)", url, exc)
        if _is_no_video_error(exc):
            error = NO_VIDEO_ERROR
        return DownloadResult(success=False, error=error)
