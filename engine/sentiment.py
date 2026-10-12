"""
Community affinity: rule-based VADER sentiment.

VADER is tuned for short social-media text, needs no model download and scores a
post in microseconds. Technical jargon it misreads as emotional ("kill a zombie
process") is neutralised below.
"""

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import config

# Emotional in everyday English, neutral jargon in software; valence 0 hides them.
_TECH_NEUTRAL = (
    'kill', 'killed', 'kills', 'killing', 'zombie', 'zombies', 'dead', 'deadlock',
    'abort', 'aborted', 'fatal', 'panic', 'panics', 'exception', 'exceptions',
    'error', 'errors', 'bug', 'bugs', 'hack', 'hacks', 'hacking', 'attack', 'attacks',
    'exploit', 'exploits', 'vulnerability', 'threat', 'threats', 'execute', 'executed',
    'block', 'blocked', 'blocking', 'fork', 'forked', 'crash', 'crashes', 'critical',
    'drop', 'dropped', 'garbage', 'master', 'slave', 'trap', 'freeze', 'frozen',
)

_analyzer = SentimentIntensityAnalyzer()
for _word in _TECH_NEUTRAL:
    _analyzer.lexicon[_word] = 0.0


def score_post(post):
    """VADER compound polarity in [-1, 1] of a post's title and opening text (0 if empty)."""
    text = f"{post.get('title') or ''}. {(post.get('content') or '')[:config.SENTIMENT_TEXT_CHARS]}"
    return _analyzer.polarity_scores(text)['compound'] if text.strip(' .') else 0.0


def summarise(compounds):
    """Aggregates post polarities into (mean, positive count, negative count)."""
    compounds = list(compounds)
    t = config.SENTIMENT_POST_THRESHOLD
    mean = sum(compounds) / len(compounds) if compounds else 0.0
    return (mean, sum(1 for c in compounds if c >= t),
            sum(1 for c in compounds if c <= -t))


def affinity(mean, positive, negative, total):
    """
    Classifies a topic's conversation as Negative, Mixed, Neutral, Positive or
    Highly positive. Mixed wins first: a split conversation is not "neutral" just
    because it averages out.
    """
    if not total:
        return "Neutral"
    share = config.SENTIMENT_MIXED_SHARE
    if positive / total >= share and negative / total >= share:
        return "Mixed"
    if mean >= config.SENTIMENT_HIGHLY_POSITIVE:
        return "Highly positive"
    if mean >= config.SENTIMENT_POST_THRESHOLD:
        return "Positive"
    if mean <= -config.SENTIMENT_POST_THRESHOLD:
        return "Negative"
    return "Neutral"
