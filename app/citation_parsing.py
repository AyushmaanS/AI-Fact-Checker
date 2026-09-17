import re

# Matches a whole [...] bracket that contains at least one SOURCE_N - not just
# "[SOURCE_1]" but also a combined "[SOURCE_1, SOURCE_2, SOURCE_3]" bracket, which
# models produce often enough that callers need to handle it, not just prompt against it.
_CITATION_BRACKET_PATTERN = re.compile(r"\[([^\]]*SOURCE_\d+[^\]]*)\]")


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def extract_citation_indices(sentence: str) -> list[int]:
    indices = []
    for bracket_content in _CITATION_BRACKET_PATTERN.findall(sentence):
        indices.extend(int(n) for n in re.findall(r"SOURCE_(\d+)", bracket_content))
    return indices
