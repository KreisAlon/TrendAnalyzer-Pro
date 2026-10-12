"""
Relevance and quality stages of the Gatekeeper, mixed into `BaseCollector`.

The planning agent may change *what* counts as on-topic (the keyword tiers), never
*how strict* the gate is: every structural threshold comes from config.py.
"""

import hashlib
import re

from langdetect import detect_langs, DetectorFactory, LangDetectException
from langdetect.detector_factory import init_factory

from config import (
    DEFAULT_STRONG_KEYWORDS, DEFAULT_WEAK_KEYWORDS, MIN_CONTENT_CHARS, MIN_WORD_COUNT,
    MIN_ALPHA_RATIO, MAX_HASHTAG_RATIO, MIN_UNIQUE_TOKEN_RATIO, REPETITION_WINDOW,
    LANG_MIN_PROB,
)
from collectors.nlp import kw_model, _KW_LOCK
from collectors.text_cleaning import JUNK_PATTERN

# langdetect is randomised per call; a fixed seed makes the language stage
# reproducible. Profiles are loaded eagerly because langdetect's lazy loader is not
# thread-safe and the interactive and background scans may race on first use.
DetectorFactory.seed = 0
init_factory()


def _build_keyword_pattern(keywords):
    """
    Compiles keywords into one case-insensitive, word-boundary alternation.

    The `\\b` anchors stop short terms ('ai', 'ml') matching inside other words.
    """
    # Longest-first, so a multi-word phrase wins over its own prefix.
    alternatives = '|'.join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    return re.compile(r'\b(?:' + alternatives + r')\b', re.IGNORECASE)


# Fallback tiers when no TopicProfile is applied.
DEFAULT_STRONG_PATTERN = _build_keyword_pattern(DEFAULT_STRONG_KEYWORDS)
DEFAULT_WEAK_PATTERN = _build_keyword_pattern(DEFAULT_WEAK_KEYWORDS)

# Matches nothing: used for an empty plan tier.
NEVER_MATCH = re.compile(r'(?!)')


class RelevanceMixin:
    """
    Topic vocabulary and the staged quality gate (`is_quality_content`).

    Relies on state and helpers provided by `BaseCollector`.
    """

    def apply_topic_profile(self, profile):
        """
        Applies the planning agent's run-specific vocabulary.

        Thresholds stay those of config.py, so a plan cannot weaken the gate. An empty
        tier becomes NEVER_MATCH, not the defaults, so an unusable topic accepts
        nothing rather than posts about an unrelated default subject.

        Args:
            profile (engine.topic_profile.TopicProfile | None): None keeps the curated defaults.
        """
        if profile is None:
            return
        self.topic_profile = profile
        # getattr: the profile is derived from untrusted model output.
        strong = getattr(profile, 'strong_keywords', None)
        weak = getattr(profile, 'weak_keywords', None)
        self._strong_pattern = _build_keyword_pattern(strong) if strong else NEVER_MATCH
        self._weak_pattern = _build_keyword_pattern(weak) if weak else NEVER_MATCH

    def planned(self, field, default):
        """
        Copy of one per-platform query list from the active plan.

        Without a plan, `default` is used; with one, an empty field yields [].
        """
        if self.topic_profile is None:
            return list(default)
        return list(getattr(self.topic_profile, field, None) or [])

    def is_topic_relevant(self, text, title=''):
        """
        Relevance stage: two-tier vocabulary test.

        One strong term anywhere suffices; weak terms need to appear in the title or
        as at least two distinct terms in the text.

        Args:
            text (str): Title and body combined.
            title (str): The title alone, for the weak-term title rule.
        """
        if not text:
            return False
        if self._strong_pattern.search(text):
            return True
        if title and self._weak_pattern.search(title):
            return True
        # Repeated occurrences of one weak term are not corroboration.
        distinct_weak = {m.group(0).lower() for m in self._weak_pattern.finditer(text)}
        return len(distinct_weak) >= 2

    @staticmethod
    def extract_keywords(text):
        """
        Final gate stage: up to five key phrases via KeyBERT.

        MMR keeps phrases diverse; phrases scoring <= 0.35 are dropped, and a post
        with none is treated as lacking analysable content.

        Returns:
            list[str]: Lower-cased key phrases (possibly empty).
        """
        if not text:
            return []
        try:
            with _KW_LOCK:
                extracted = kw_model.extract_keywords(
                    text,
                    keyphrase_ngram_range=(1, 2),
                    stop_words='english',
                    top_n=8,
                    use_mmr=True,
                    diversity=0.6,
                )
            keywords = [phrase.lower() for phrase, score in extracted
                        if isinstance(phrase, str) and isinstance(score, (int, float))
                        and score > 0.35]
            return keywords[:5]
        except Exception as e:
            # Fault Isolation: a model failure rejects this post, never the run.
            print(f"Extraction error: {e}")
            return []

    def _structural_reject(self, text):
        """
        Structural stage: cheap checks run before any model sees the text.

        Returns:
            str | None: Rejection reason code, or None when the text looks like prose.
        """
        # Bot walls and paywalls served with HTTP 200 announce themselves early.
        if JUNK_PATTERN.search(text[:400]):
            return 'junk_page'
        if len(text) < self.min_content_chars:
            return 'too_short'
        tokens = text.split()
        # Word floor scales with the collector's length floor, never below eight words.
        if len(tokens) < max(8, MIN_WORD_COUNT * self.min_content_chars // MIN_CONTENT_CHARS):
            return 'too_few_words'
        if sum(c.isalpha() or c.isspace() for c in text) / len(text) < MIN_ALPHA_RATIO:
            return 'low_alpha_ratio'
        if sum(t.startswith('#') for t in tokens) / len(tokens) > MAX_HASHTAG_RATIO:
            return 'hashtag_soup'
        # Fixed window keeps lexical diversity independent of length.
        words = [t.lower() for t in tokens[:REPETITION_WINDOW] if t.isalpha()]
        if len(words) >= 20 and len(set(words)) / len(words) < MIN_UNIQUE_TOKEN_RATIO:
            return 'repetitive'
        return None

    def is_quality_content(self, post):
        """
        The Gatekeeper: decides whether one normalised post is stored.

        Stages run cheapest-first, so the models only see text that survived the
        cheap checks:

            1. structure  -> _structural_reject()
            2. relevance  -> is_topic_relevant()
            3. uniqueness -> SHA-256 fingerprint of the normalised body
            4. language   -> langdetect, English with probability >= LANG_MIN_PROB
            5. themes     -> extract_keywords() must return at least one phrase

        The first failing stage records its reason, so each rejection is counted
        once. Accepted posts gain `keywords` and a normalised `published_at` in place.
        """
        post['title'] = self.clean_text(post.get('title', ''))
        post['content'] = self.clean_text(post.get('content', ''))
        full_text = f"{post['title']} {post['content']}"

        # Body only: Mastodon derives its title from the body, which would count twice.
        reason = self._structural_reject(post['content'])
        if reason:
            self.reject_reasons[reason] += 1
            return False

        if not self.is_topic_relevant(full_text, post['title']):
            self.reject_reasons['not_topic_relevant'] += 1
            return False

        # Ignores case, punctuation and whitespace, so re-formatted copies match.
        content_hash = hashlib.sha256(
            re.sub(r'\W+', '', post['content'].lower()).encode('utf-8')
        ).hexdigest()
        if content_hash in self._seen_hashes:
            self.reject_reasons['duplicate'] += 1
            return False

        # Very short samples are skipped: the detector is unreliable on them.
        text_to_check = full_text[:500]
        if len(text_to_check) > 25:
            try:
                langs = detect_langs(text_to_check)
                if langs[0].lang != 'en' or langs[0].prob < LANG_MIN_PROB:
                    self.reject_reasons['not_english'] += 1
                    return False
            except (LangDetectException, IndexError):
                # IndexError: detect_langs() may return no language above its threshold.
                self.reject_reasons['lang_undetectable'] += 1
                return False

        post['keywords'] = self.extract_keywords(full_text)
        if not post['keywords']:
            self.reject_reasons['no_keywords'] += 1
            return False

        post['published_at'] = self.to_iso8601(post.get('published_at'))
        # Registered only on acceptance, so a rejected post cannot block a valid copy.
        self._seen_hashes.add(content_hash)
        return True
