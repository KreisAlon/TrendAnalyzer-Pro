"""
Search-plan contract between the planner (engine/planning.py) and the collectors
(collectors/base.py); a separate module keeps the import graph acyclic.

A profile is the only route from user input to HTTP requests and the quality gate,
so every derived query and keyword is validated and unusable values are dropped:
a topic can never inject query syntax.
"""

import contextlib
from dataclasses import dataclass, field, asdict
import json
import os
import re

import config

# Each query is a real HTTP request per cycle, so these caps bound a plan's cost.
MAX_QUERIES = 8
MAX_TAGS = 6
MAX_KEYWORDS = 40
MAX_QUERY_CHARS = 60

# Single characters match almost everywhere; two keeps genuine terms like 'ai'.
MIN_KEYWORD_CHARS = 2

# GitHub topic slug grammar (anything else becomes loose free-text search).
_TOPIC_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,34}$')

# Mastodon / Dev.to tag grammar: no hyphens.
_TAG_RE = re.compile(r'^[a-z0-9]{2,30}$')

# Keywords become \b...\b patterns, so no regex metacharacters; max 40 chars.
_KEYWORD_RE = re.compile(r'^[a-z0-9][a-z0-9 .\-]{0,39}$')

_SEARCH_STOPWORDS = frozenset(
    "a an and or the of for in on to with about into from by vs versus new latest".split()
)


def _sanitise(values, limit, normalise, is_valid, key=None):
    """
    Filters an untrusted list to valid, de-duplicated items, preserving order
    (collectors treat position as priority).

    Args:
        values: Untrusted value expected to be a list of strings.
        limit (int): Maximum number of items returned.
        normalise (callable): str -> str, applied before validation.
        is_valid (callable): str -> bool grammar check.
        key (callable | None): De-duplication key; defaults to the item itself.
    """
    if not isinstance(values, (list, tuple)):
        return []
    seen, out = set(), []
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = normalise(raw)
        dedupe_key = key(item) if key else item
        if not item or dedupe_key in seen or not is_valid(item):
            continue
        seen.add(dedupe_key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _clean_list(values, pattern, limit, transform=None):
    """Lower-cases untrusted items and keeps those fully matching `pattern`."""
    def normalise(raw):
        item = raw.strip().lower()
        return transform(item) if transform else item
    return _sanitise(values, limit, normalise, pattern.match)


def _clean_keywords(values, limit=MAX_KEYWORDS):
    """Sanitises a keyword tier so every term is safe for word-boundary matching."""
    def strip_punct(item):
        # Salvage wrapped terms ('"llm"', '#llm', 'llm,') instead of dropping them.
        return item.strip('"\'`#,;:()[]{}').strip()

    return [k for k in _clean_list(values, _KEYWORD_RE, limit, transform=strip_punct)
            if len(k) >= MIN_KEYWORD_CHARS]


def _clean_free_queries(values, limit=MAX_QUERIES):
    """
    Sanitises free-text Algolia (Hacker News) queries. Looser than tag grammars
    (sent as a query parameter); whitespace is collapsed, casing preserved.
    """
    return _sanitise(
        values, limit,
        normalise=lambda raw: re.sub(r'\s+', ' ', raw).strip().strip('"\''),
        is_valid=lambda q: len(q) <= MAX_QUERY_CHARS,
        key=str.lower,
    )


@dataclass
class TopicProfile:
    """
    One run's search plan: per-platform queries plus relevance keywords.

    A strong keyword suffices anywhere in a post; a weak one only in a title or
    alongside a second distinct weak keyword.

    Attributes:
        target_topic: The topic the plan was built for.
        strong_keywords / weak_keywords: The two relevance tiers.
        github_topics / hn_queries / devto_tags / mastodon_tags: Per-platform queries.
        source: Provenance -- "keywords" or "fallback" -- shown in the UI.
        rationale: One-sentence scoping note (persisted with the plan).
        generated_at: ISO8601 UTC creation timestamp (persisted with the plan).
    """
    target_topic: str = config.TARGET_TOPIC
    strong_keywords: list = field(default_factory=list)
    weak_keywords: list = field(default_factory=list)
    github_topics: list = field(default_factory=list)
    hn_queries: list = field(default_factory=list)
    devto_tags: list = field(default_factory=list)
    mastodon_tags: list = field(default_factory=list)
    source: str = "fallback"
    rationale: str = ""
    generated_at: str = ""

    @classmethod
    def default(cls, target_topic=None):
        """The curated plan from config.py (source "fallback") for the default topic."""
        # Defensive copies: mutating the profile must never alter config.
        return cls(
            target_topic=target_topic or config.TARGET_TOPIC,
            strong_keywords=list(config.DEFAULT_STRONG_KEYWORDS),
            weak_keywords=list(config.DEFAULT_WEAK_KEYWORDS),
            github_topics=list(config.DEFAULT_GITHUB_TOPICS),
            hn_queries=list(config.DEFAULT_HN_QUERIES),
            devto_tags=list(config.DEFAULT_DEVTO_TAGS),
            mastodon_tags=list(config.DEFAULT_MASTODON_TAGS),
            source="fallback",
            rationale="Static curated keyword set from config.py.",
        )

    @classmethod
    def empty(cls, target_topic):
        """A plan that searches for nothing (`is_empty()` is True)."""
        return cls(target_topic=target_topic, source="keywords",
                   rationale="Not a searchable topic.")

    def is_empty(self):
        """True when no platform has anything to search for."""
        return not (self.github_topics or self.hn_queries or self.devto_tags
                    or self.mastodon_tags)

    @classmethod
    def from_search_terms(cls, target_topic):
        """
        Derives a plan deterministically from the user's words: the phrase (plus
        hyphenated and compact spellings) is the strong tier, individual words the
        weak tier. The default topic gets the curated plan; no usable words yields
        an empty plan.
        """
        topic = (target_topic or '').strip()
        if not topic or topic.lower() == config.TARGET_TOPIC.strip().lower():
            return cls.default(topic or None)

        # Technology-name-friendly tokens ('c++', 'node.js'), minus filler words.
        words = [w for w in re.findall(r'[a-z0-9][a-z0-9.+\-]*', topic.lower())
                 if w not in _SEARCH_STOPWORDS]
        words = [w for w in (t.strip('.-+') for t in words) if len(w) >= MIN_KEYWORD_CHARS]
        if not words:
            return cls.empty(topic)

        # Three spellings of the phrase, because platforms tag inconsistently:
        # "machine learning", "machine-learning" (GitHub) and "machinelearning" (tags).
        phrase = " ".join(words)
        alnum = [re.sub(r'[^a-z0-9]', '', w) for w in words]
        compact, hyphenated = "".join(alnum), "-".join(alnum)

        strong = _clean_keywords([phrase, hyphenated, compact])
        # An empty weak tier would never match, so a single word stands in for it.
        weak = _clean_keywords(words if len(words) > 1 else [phrase])

        return cls(
            target_topic=topic,
            strong_keywords=strong,
            weak_keywords=weak,
            github_topics=_clean_list([hyphenated, compact] + words, _TOPIC_SLUG_RE,
                                      MAX_QUERIES),
            hn_queries=_clean_free_queries([topic] + (words if len(words) > 1 else [])),
            devto_tags=_clean_list([compact] + words, _TAG_RE, MAX_TAGS),
            mastodon_tags=_clean_list([compact] + words, _TAG_RE, MAX_TAGS),
            source="keywords",
            rationale="Searched directly for the words in your topic.",
        )

    def to_dict(self):
        """Returns the plan as a JSON-serialisable dict."""
        return asdict(self)

    def save(self, path=None):
        """
        Persists the plan (defaults to config.PROFILE_PATH) atomically via a temp file
        and `os.replace`. Failures are logged, not raised: the scan already succeeded.
        """
        path = path or config.PROFILE_PATH
        tmp = f"{path}.tmp"
        try:
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self.to_dict(), fh, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            print(f"   ⚠️  Could not persist topic profile: {exc}")
            with contextlib.suppress(OSError):
                os.remove(tmp)

    @classmethod
    def load(cls, path=None):
        """
        Reads a persisted plan (defaults to config.PROFILE_PATH); None when missing,
        unreadable or not a JSON object.
        """
        try:
            with open(path or config.PROFILE_PATH, encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, ValueError):  # ValueError covers JSON and UTF-8 decode errors
            return None
        if not isinstance(data, dict):
            return None
        # Unknown keys (from a newer schema) are ignored.
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
