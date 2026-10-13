"""
Text-cleaning patterns shared by the quality gate and the platform collectors.
"""

import re

from config import JUNK_PATTERNS

# Shared by the gate and by early-exit checks in collectors (e.g. the HN crawler).
JUNK_PATTERN = re.compile('|'.join(re.escape(p) for p in JUNK_PATTERNS), re.IGNORECASE)

# The lookbehind requires a lowercase letter, digit, bracket or quote before the
# terminator, so initials and version numbers ("U.S.", "v1.5") are not sentence ends.
_SENTENCE_END_RE = re.compile(r'(?<=[a-z0-9\)\]"\'])[.!?][\s]')

# Maximum share of the budget a boundary-aware cut may discard.
_MIN_BOUNDARY_RATIO = 0.75

# Markdown boilerplate that carries no semantic signal. Stripped before truncation so
# the text budget is spent on prose and key phrases describe the content, not badges.
_FRONTMATTER_RE = re.compile(r'\A---\n.*?\n---\n', re.S)
_CODE_FENCE_RE = re.compile(r'```.*?```', re.S)
_HTML_COMMENT_RE = re.compile(r'<!--.*?-->', re.S)
_BADGE_RE = re.compile(r'\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)')   # [![alt](img)](link)
_IMAGE_RE = re.compile(r'!\[[^\]]*\]\([^)]*\)')                # ![alt](img)
_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]*\)')                # [text](url) -> text
_HEADING_RE = re.compile(r'^\s{0,3}#{1,6}\s*', re.M)
_TABLE_ROW_RE = re.compile(r'^\s*\|.*\|\s*$', re.M)
_HRULE_RE = re.compile(r'^\s*[-=*_]{3,}\s*$', re.M)


def clean_markdown(text):
    """
    Reduces a markdown document (GitHub README, Dev.to article) to plain prose.

    Returns:
        str: Whitespace-normalised prose with markup removed.
    """
    if not text:
        return ""
    # Whole non-prose regions first, then badges before images, since a badge is an
    # image nested inside a link.
    for pattern in (_FRONTMATTER_RE, _HTML_COMMENT_RE, _CODE_FENCE_RE, _BADGE_RE, _IMAGE_RE):
        text = pattern.sub(' ', text)
    text = _LINK_RE.sub(r'\1', text)
    text = _HEADING_RE.sub('', text)
    text = _TABLE_ROW_RE.sub(' ', text)
    text = _HRULE_RE.sub(' ', text)
    return re.sub(r'\s+', ' ', text).strip()
