"""Per-platform text signals for the background feed: KeyBERT buzzwords and VADER sentiment."""

import re
from collections import Counter

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

from engine import sentiment

BUZZWORDS_PER_PLATFORM = 8
# One or two words of plain lowercase letters: no digits, symbols or fragments.
_BUZZWORD_RE = re.compile(r'^[a-z]{3,}(?: [a-z]{3,})?$')
# Conversational filler that sklearn's English stop-word list does not cover.
_FILLER_WORDS = frozenset({
    'just', 'really', 'actually', 'basically', 'simply', 'pretty', 'like', 'got',
    'get', 'gets', 'make', 'makes', 'use', 'using', 'used', 'new', 'way', 'thing',
    'things', 'lot', 'lots', 'need', 'want', 'know', 'think', 'good', 'great',
})


def clean_buzzword(phrase):
    """
    Returns `phrase` normalised if it is a clean buzzword, else None. Drops what
    KeyBERT still lets through: digits/symbols, stop or filler words, repeats.
    """
    phrase = re.sub(r'\s+', ' ', str(phrase or '')).strip().lower()
    if not _BUZZWORD_RE.match(phrase):
        return None
    words = phrase.split()
    if (any(w in ENGLISH_STOP_WORDS or w in _FILLER_WORDS for w in words)
            or len(set(words)) < len(words)):
        return None
    return phrase


def _platform_buzzwords(posts):
    """platform -> most frequent clean phrases, counted once per post."""
    counts = {}
    for post in posts:
        phrases = {clean_buzzword(k) for k in post.get('keywords') or []} - {None}
        counts.setdefault(post['source_platform'], Counter()).update(phrases)
    return {platform: [k for k, _ in c.most_common(BUZZWORDS_PER_PLATFORM)]
            for platform, c in counts.items()}


def _platform_sentiment(posts):
    """platform -> (mean VADER compound, positive posts, negative posts)."""
    compounds = {}
    for post in posts:
        compounds.setdefault(post['source_platform'], []).append(sentiment.score_post(post))
    return {platform: sentiment.summarise(values) for platform, values in compounds.items()}
