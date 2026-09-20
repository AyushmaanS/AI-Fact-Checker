from unittest.mock import patch

from app.ingestion.media_downloader import DOWNLOAD_FAILED_TEXT, download_media


@patch("app.ingestion.media_downloader._find_downloaded_file")
@patch("app.ingestion.media_downloader._extract_sync")
async def test_download_media_success(mock_extract, mock_find_file):
    mock_extract.return_value = {"description": "A caption from the post #news"}
    mock_find_file.return_value = "/tmp/ingest_xyz/abc123.mp4"

    result = await download_media("https://www.instagram.com/reel/abc123/")

    assert result.success is True
    assert result.file_path == "/tmp/ingest_xyz/abc123.mp4"
    assert result.caption == "A caption from the post #news"
    assert result.error is None
    mock_extract.assert_called_once()
    assert mock_extract.call_args.args[1] is True  # download=True on the one and only call


@patch("app.ingestion.media_downloader._find_downloaded_file")
@patch("app.ingestion.media_downloader._extract_sync")
async def test_download_media_falls_back_to_caption_when_video_download_fails(mock_extract, mock_find_file):
    # First (download=True) call fails, second (download=False, metadata-only)
    # call succeeds with a caption - the exact scenario the caption-only
    # fallback exists for (e.g. a CDN-specific rate limit).
    mock_extract.side_effect = [
        RuntimeError("HTTP Error 429: Too Many Requests"),
        {"description": "Still got the caption though #breaking"},
    ]

    result = await download_media("https://www.instagram.com/reel/rate-limited/")

    assert result.success is False
    assert result.file_path is None
    assert result.caption == "Still got the caption though #breaking"
    assert result.error == DOWNLOAD_FAILED_TEXT
    assert mock_extract.call_count == 2
    mock_find_file.assert_not_called()


@patch("app.ingestion.media_downloader._extract_sync")
async def test_download_media_reports_clean_failure_when_both_attempts_fail(mock_extract):
    # A genuinely private/deleted post - neither the video nor its metadata is
    # reachable at all.
    mock_extract.side_effect = RuntimeError("Private video")

    result = await download_media("https://www.instagram.com/reel/private-post/")

    assert result.success is False
    assert result.file_path is None
    assert result.caption is None
    assert result.error == DOWNLOAD_FAILED_TEXT
    assert mock_extract.call_count == 2


@patch("app.ingestion.media_downloader._find_downloaded_file")
@patch("app.ingestion.media_downloader._extract_sync")
async def test_download_media_falls_back_if_no_file_was_actually_written(mock_extract, mock_find_file):
    # yt-dlp claimed success (no exception) but somehow nothing landed on disk -
    # still treated as a failure, falling through to the metadata-only attempt,
    # not returned as a false "success" with a nonexistent file_path.
    mock_extract.side_effect = [
        {"description": "should not surface"},
        {"description": "from the metadata-only retry"},
    ]
    mock_find_file.return_value = None

    result = await download_media("https://www.instagram.com/reel/weird-case/")

    assert result.success is False
    assert result.caption == "from the metadata-only retry"
    assert mock_extract.call_count == 2
