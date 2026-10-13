"""
Abstract collector and the shared quality gate ("the Gatekeeper").

    search API -> candidate pool -> concurrent deep fetch
        -> quality gate (structure, relevance, uniqueness, language, key phrases)
        -> accepted posts + funnel accounting -> engine -> SQLite

The planning agent may change *what* counts as on-topic, never *how strict* the
gate is: structural thresholds, the language floor and the duplicate fingerprint
are fixed in config.py. Rejections are tallied in `reject_reasons` and summarised
by `volume_report()` for the Gatekeeper tab.

Mixins: RelevanceMixin (relevance.py, the gate) and FunnelMixin (funnel.py,
candidate pool, waves, volume report).
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from collections import Counter
import re
import html

from config import (
    MIN_CONTENT_CHARS, TARGET_POSTS_PER_PLATFORM, MAX_POSTS_PER_PLATFORM,
    CANDIDATE_OVERSAMPLE,
)
from collectors.funnel import FunnelMixin
from collectors.relevance import (RelevanceMixin, DEFAULT_STRONG_PATTERN,
                                  DEFAULT_WEAK_PATTERN)
from collectors.text_cleaning import _SENTENCE_END_RE, _MIN_BOUNDARY_RATIO


class BaseCollector(RelevanceMixin, FunnelMixin, ABC):
    """
    Template for a platform collector. Subclasses implement only `collect()`;
    gating and funnel bookkeeping live here once.
    """

    def __init__(self, platform_name, max_posts=None):
        self.platform_name = platform_name
        # Per-scan limits: accepted-post target and the oversampled candidate pool.
        self.target_posts = TARGET_POSTS_PER_PLATFORM
        self.max_candidates = MAX_POSTS_PER_PLATFORM
        if max_posts:
            self.set_max_posts(max_posts)
        # Subclasses may lower this when records are short but curated (GitHub).
        self.min_content_chars = MIN_CONTENT_CHARS
        # Per-run vocabulary; replaced in memory by apply_topic_profile().
        self.topic_profile = None
        self._strong_pattern = DEFAULT_STRONG_PATTERN
        self._weak_pattern = DEFAULT_WEAK_PATTERN
        # Content fingerprints accepted this run (de-duplicates across external ids).
        self._seen_hashes = set()
        self.reject_reasons = Counter()
        # Funnel state for volume_report(): what the platform reported and how the
        # gate treated inspected candidates (collectors stop at the target).
        self.search_totals = []        # Platform-reported match counts, one per query
        self.search_capped = False     # A total hit the platform's reporting ceiling
        self.fetched_rows = 0          # Candidate rows returned across all queries
        self.unique_candidates = 0     # ...after cross-query de-duplication
        self.overlap_factor = None     # Override when overlap is measured another way
        self.gated = 0                 # Candidates run through the gate
        self.gate_accepted = 0         # ...that passed, before the target cap

    def set_max_posts(self, max_posts):
        """Sets this scan's per-platform post target (and the candidate pool with it)."""
        self.target_posts = max(1, int(max_posts))
        self.max_candidates = self.target_posts * CANDIDATE_OVERSAMPLE

    @staticmethod
    def clean_text(text):
        """Decodes entities, strips tags and URLs, and collapses whitespace."""
        if not text:
            return ""
        clean = html.unescape(str(text))
        # Tags become spaces so adjacent words do not fuse.
        clean = re.sub(r'<[^>]+>', ' ', clean)
        # URLs carry no topical words and distort length metrics.
        clean = re.sub(r'(?:https?://|www\.)\S+', '', clean)
        return re.sub(r'\s+', ' ', clean).strip()

    @staticmethod
    def truncate_at_boundary(text, limit):
        """
        Cuts `text` to at most `limit` chars (<= 0 disables), preferring a sentence
        end, then a word break, then a hard cut. A boundary cut may discard at most a
        quarter of the budget. No ellipsis is appended.
        """
        if not text:
            return ""
        text = text.strip()
        if limit <= 0 or len(text) <= limit:
            return text

        head = text[:limit]
        floor = int(limit * _MIN_BOUNDARY_RATIO)

        matches = list(_SENTENCE_END_RE.finditer(head))
        if matches and matches[-1].start() + 1 >= floor:
            return head[:matches[-1].start() + 1].strip()

        space = head.rfind(' ')
        if space >= floor:
            return head[:space].strip()
        # Degenerate case (one enormous token): a hard cut is the only option.
        return head.strip()

    @staticmethod
    def to_iso8601(value):
        """
        Normalises a Unix epoch or ISO string to ISO 8601 UTC.

        Returns:
            str: ISO 8601 UTC; '' when missing or out of range; the original string
            when unparseable (the dashboard coerces it to NaT).
        """
        if value in (None, ''):
            return ''
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            try:
                return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                return ''
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            # Naive timestamps are assumed to already be UTC.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            return str(value)

    @abstractmethod
    async def collect(self, client):
        """
        Collects, gates and returns this platform's posts for one scan.

        Args:
            client (httpx.AsyncClient): Shared HTTP client for the whole scan.

        Returns:
            list[dict]: Accepted posts in the unified schema.
        """
