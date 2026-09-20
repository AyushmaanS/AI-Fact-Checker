import re

# Unicode-aware by default in Python's re (str patterns aren't ASCII-only), so
# a non-English hashtag is still picked up.
HASHTAG_PATTERN = re.compile(r"#(\w+)")
URL_PATTERN = re.compile(r"https?://\S+")


def extract_caption_content(raw_caption: str | None) -> tuple[str, list[str]]:
    """Per functional-spec §B.3's Caption Path row (post metadata/text -> caption
    + topics): trims the caption, and pulls hashtags/links out of it as
    topic-context signals. Doesn't dedupe - a repeated hashtag is still a real
    repeated signal, not noise to collapse."""
    caption = (raw_caption or "").strip()
    topics = HASHTAG_PATTERN.findall(caption) + URL_PATTERN.findall(caption)
    return caption, topics
