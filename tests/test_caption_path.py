from app.ingestion.caption_path import extract_caption_content


def test_extract_caption_content_pulls_hashtags_and_links():
    caption, topics = extract_caption_content(
        "Check this out! #factcheck #BreakingNews https://example.com/article"
    )
    assert caption == "Check this out! #factcheck #BreakingNews https://example.com/article"
    assert topics == ["factcheck", "BreakingNews", "https://example.com/article"]


def test_extract_caption_content_trims_whitespace():
    caption, _ = extract_caption_content("   spaced out caption   ")
    assert caption == "spaced out caption"


def test_extract_caption_content_handles_none():
    caption, topics = extract_caption_content(None)
    assert caption == ""
    assert topics == []


def test_extract_caption_content_handles_no_hashtags_or_links():
    caption, topics = extract_caption_content("Just a plain caption with no signals.")
    assert caption == "Just a plain caption with no signals."
    assert topics == []


def test_extract_caption_content_keeps_repeated_hashtags():
    _, topics = extract_caption_content("#news is #news, more #news")
    assert topics == ["news", "news", "news"]
