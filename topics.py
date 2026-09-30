"""
The topic contract shared between the agentic planner and the collectors.

Why this lives in its own module
--------------------------------
`DynamicTopicManager` (engine.py) *produces* a profile and `BaseCollector`
(collectors/base.py) *consumes* one. Putting the shared type in either of those
would make the import cycle main -> collectors -> main, so the data contract sits
here and both ends depend on it rather than on each other.

Everything arriving from the model is untrusted
-----------------------------------------------
A profile is the only place where model output reaches the request layer and the
Gatekeeper, so `TopicProfile.from_payload()` is written as a sanitiser first and a
parser second. It never raises: any field that is missing, empty, wrongly typed or
syntactically unusable falls back to the hand-curated default from config.py, so a
hallucinated or truncated response degrades the run to the previous hardcoded
behaviour instead of breaking ingestion or silently searching for nothing.
"""

from dataclasses import dataclass, field, asdict
import json
import os
import re

import config

# --- Sanitisation bounds ----------------------------------------------------
# Upper bounds on how much of a model response we are willing to act on. Each
# extra GitHub topic or Algolia query is a real HTTP request per cycle, so an
# over-enthusiastic response must not multiply the network cost of a run.
MAX_QUERIES = 8
MAX_TAGS = 6
MAX_KEYWORDS = 40

# A keyword shorter than this cannot survive the word-boundary matcher usefully:
# single letters match constantly. Two characters is the floor because real terms
# of that length exist and matter here ('ai', 'ml').
MIN_KEYWORD_CHARS = 2
MAX_KEYWORD_CHARS = 40

# GitHub topic slugs are lowercase alphanumerics plus hyphens. Anything else in
# that field (a phrase, a boolean expression, a quoted string) is not a topic and
# would be parsed by GitHub's search as loose free text, which is exactly the
# silent-failure mode the curated query list was written to avoid.
_TOPIC_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,34}$')

# Mastodon and Dev.to tags are single lowercase alphanumeric tokens: Mastodon has
# no hyphens in hashtags at all, and Dev.to normalises them away.
_TAG_RE = re.compile(r'^[a-z0-9]{2,30}$')

# Keywords are matched with \b...\b, so a term containing regex metacharacters or
# newlines either fails to compile or matches unpredictably. Letters, digits,
# spaces, hyphens and dots ('gpt-4', 'node.js') are the whole alphabet we allow.
_KEYWORD_RE = re.compile(r'^[a-z0-9][a-z0-9 .\-]{0,39}$')

# Filler words dropped when a plan is built straight from the user's search text.
_SEARCH_STOPWORDS = frozenset(
    "a an and or the of for in on to with about into from by vs versus new latest".split()
)

PROFILE_PATH =os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "topic_profile.json")


def _clean_list(values, pattern, limit, transform=None):
    """
    Filters an untrusted list down to items matching `pattern`, de-duplicated,
    order-preserving, and capped at `limit`.

    Order is preserved because the model is asked to return its queries
    best-first and the collectors treat position as priority -- GitHub's first
    topics get the higher star floor. Sorting or de-duplicating through a set
    would throw that ranking away.
    """
    if not isinstance(values, (list, tuple)):
        return []
    seen, out = set(), []
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = raw.strip().lower()
        if transform:
            item = transform(item)
        if not item or item in seen or not pattern.match(item):
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _clean_keywords(values, limit=MAX_KEYWORDS):
    """Keyword-tier sanitiser: length-bounded and regex-safe for \\b matching."""
    def strip_punct(item):
        # Models commonly return '"llm"', '#llm' or 'llm,'. Salvage the term
        # rather than discarding an otherwise valid keyword on punctuation.
        return item.strip('"\'`#,;:()[]{}').strip()

    return [
        k for k in _clean_list(values, _KEYWORD_RE, limit, transform=strip_punct)
        if MIN_KEYWORD_CHARS <= len(k) <= MAX_KEYWORD_CHARS
    ]


def _clean_free_queries(values, limit=MAX_QUERIES):
    """
    Sanitiser for free-text search strings (Algolia). Looser than the tag and
    slug tiers -- these are passed as a query parameter, not interpolated into a
    query language -- but still bounded in length and stripped of control
    characters so a multi-line response cannot become several parameters.
    """
    if not isinstance(values, (list, tuple)):
        return []
    seen, out = set(), []
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = re.sub(r'\s+', ' ', raw).strip().strip('"\'')
        if not item or len(item) > 60 or item.lower() in seen:
            continue
        seen.add(item.lower())
        out.append(item)
        if len(out) >= limit:
            break
    return out


@dataclass
class TopicProfile:
    """
    One run's search plan: what to ask each platform for, and what counts as
    relevant once the answers come back.

    The two keyword tiers keep the semantics the Gatekeeper already relies on --
    a strong term is accepted anywhere, a weak term needs a title position or a
    second distinct weak term -- so swapping the vocabulary per run does not
    change how strictly it is applied.
    """
    target_topic: str = "AI and machine learning"
    strong_keywords: list = field(default_factory=list)
    weak_keywords: list = field(default_factory=list)
    github_topics: list = field(default_factory=list)
    hn_queries: list = field(default_factory=list)
    devto_tags: list = field(default_factory=list)
    mastodon_tags: list = field(default_factory=list)
    # Provenance, surfaced in the dashboard so a reader can tell an agentic plan
    # from the static fallback without reading the logs.
    source: str = "fallback"
    rationale: str = ""
    generated_at: str = ""

    # --- Construction -------------------------------------------------------

    @classmethod
    def default(cls, target_topic=None):
        """
        The hand-curated profile: exactly the behaviour of the previous hardcoded
        pipeline. This is the fallback whenever the planner cannot produce a
        usable plan, which is what keeps a missing API key from being fatal.
        """
        return cls(
            target_topic=target_topic or config.TARGET_TOPIC,
            strong_keywords=list(config.AI_STRONG_KEYWORDS),
            weak_keywords=list(config.AI_WEAK_KEYWORDS),
            github_topics=list(config.DEFAULT_GITHUB_TOPICS),
            hn_queries=list(config.DEFAULT_HN_QUERIES),
            devto_tags=list(config.DEFAULT_DEVTO_TAGS),
            mastodon_tags=list(config.DEFAULT_MASTODON_TAGS),
            source="fallback",
            rationale="Static curated keyword set from config.py.",
        )

    @classmethod
    def empty(cls, target_topic):
        """A plan that searches for nothing: every collector returns zero posts."""
        return cls(target_topic=target_topic, source="keywords",
                   rationale="Not a searchable topic.")

    def is_empty(self):
        """True when there is nothing to search for on any platform."""
        return not (self.github_topics or self.hn_queries or self.devto_tags
                    or self.mastodon_tags)

    @classmethod
    def from_search_terms(cls, target_topic):
        """
        A plan built directly from the words the user typed, for when the planner
        is unavailable.

        The curated default is an AI vocabulary, so falling back to it for a search
        like "electric vehicles" would collect AI posts and label them as EVs. This
        searches for the user's own terms instead: the whole phrase is the strong
        tier and its individual words are the weak tier, so a post must either
        name the topic outright or mention it in its title / use two of its words.
        Returns the curated default only for the configured default topic. A search
        with no usable words gets an EMPTY plan, so every collector returns nothing
        -- never the curated AI vocabulary, which would answer an unrelated search
        with a generic AI feed.
        """
        topic = (target_topic or '').strip()
        if not topic or topic.lower() == config.TARGET_TOPIC.strip().lower():
            return cls.default(topic or None)

        words = [w for w in re.findall(r'[a-z0-9][a-z0-9.+\-]*', topic.lower())
                 if w not in _SEARCH_STOPWORDS]
        words = [w.strip('.-+') for w in words if len(w.strip('.-+')) >= MIN_KEYWORD_CHARS]
        if not words:
            return cls.empty(topic)

        phrase = " ".join(words)
        compact = "".join(re.sub(r'[^a-z0-9]', '', w) for w in words)
        hyphenated = "-".join(re.sub(r'[^a-z0-9]', '', w) for w in words)

        strong = _clean_keywords([phrase, hyphenated, compact])
        # A single-word search has no separate words to corroborate with, and an
        # empty weak tier would be discarded by the collectors, so the word itself
        # stands in (it is already accepted anywhere through the strong tier).
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

    @classmethod
    def from_payload(cls, payload, target_topic, source="gemini"):
        """
        Builds a profile from an untrusted mapping, field by field.

        Each field falls back INDEPENDENTLY: a response with good keywords but a
        garbled `devto_tags` yields the model's keywords and tags built from the
        search words, rather than discarding the whole plan. The fallback is the
        user's own terms, not the curated AI set, so a partial answer can never
        widen an unrelated search into an AI feed.

        A payload with `"recognised": false` is the model saying the topic is not
        a real subject (gibberish, random letters); that yields an empty plan.
        """
        base = cls.from_search_terms(target_topic)
        if not isinstance(payload, dict):
            return base
        if payload.get('recognised') is False:
            return cls.empty(target_topic)

        strong = _clean_keywords(payload.get('strong_keywords'))
        weak = _clean_keywords(payload.get('weak_keywords'))

        # A weak term that also appears in the strong tier would be accepted
        # anywhere anyway, so leaving it in the weak list only inflates the
        # distinct-weak-term count that the two-hit rule depends on.
        strong_set = set(strong)
        weak = [w for w in weak if w not in strong_set]

        # The tiers are only meaningful together: with a strong list and no weak
        # list the corroboration rule can never fire, and with a weak list and no
        # strong list every acceptance needs corroboration. If either tier came
        # back unusable, take BOTH from the search-term plan.
        if not strong or not weak:
            strong, weak = base.strong_keywords, base.weak_keywords

        queries = _clean_free_queries(payload.get('hn_queries')
                                      or payload.get('search_queries'))

        return cls(
            target_topic=target_topic,
            strong_keywords=strong,
            weak_keywords=weak,
            github_topics=_clean_list(payload.get('github_topics'), _TOPIC_SLUG_RE,
                                      MAX_QUERIES) or base.github_topics,
            hn_queries=queries or base.hn_queries,
            devto_tags=_clean_list(payload.get('devto_tags'), _TAG_RE,
                                   MAX_TAGS) or base.devto_tags,
            mastodon_tags=_clean_list(payload.get('mastodon_tags'), _TAG_RE,
                                      MAX_TAGS) or base.mastodon_tags,
            source=source,
            rationale=str(payload.get('rationale') or '')[:400],
        )

    # --- Reporting ----------------------------------------------------------

    def summary(self):
        """One-line description of the plan, for the cycle log."""
        return (f"topic={self.target_topic!r} source={self.source} "
                f"strong={len(self.strong_keywords)} weak={len(self.weak_keywords)} "
                f"gh={len(self.github_topics)} hn={len(self.hn_queries)} "
                f"devto={len(self.devto_tags)} mastodon={len(self.mastodon_tags)}")

    def to_dict(self):
        return asdict(self)

    def save(self, path=PROFILE_PATH):
        """
        Persists the plan beside the database so the dashboard can name the topic
        the corpus was actually collected under.

        Written atomically through a temp file and a rename: the Streamlit app
        polls this path, and a partial write would surface as a JSONDecodeError
        in the UI. Failures are swallowed -- the plan is already in memory and the
        cycle must not die over a cache file.
        """
        try:
            tmp = f"{path}.tmp"
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self.to_dict(), fh, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            print(f"   ⚠️  Could not persist topic profile: {exc}")

    @classmethod
    def load(cls, path=PROFILE_PATH):
        """Reads a persisted profile, or None when there is nothing usable."""
        try:
            with open(path, encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
