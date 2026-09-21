from abc import ABC, abstractmethod
from datetime import datetime, timezone
from collections import Counter
import sqlite3
import hashlib
import math
import os
import re
import html  # Added for unescaping HTML entitiesֿ
import numpy as np
from textblob import TextBlob
from langdetect import detect, detect_langs, LangDetectException
from config import (
    AI_FILTER_KEYWORDS, AI_STRONG_KEYWORDS, AI_WEAK_KEYWORDS, JUNK_PATTERNS,
    MIN_CONTENT_CHARS, MIN_WORD_COUNT, MIN_ALPHA_RATIO, MAX_HASHTAG_RATIO,
    MIN_UNIQUE_TOKEN_RATIO, REPETITION_WINDOW, LANG_MIN_PROB,
)
from keybert import KeyBERT

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "trends_project.db")

print("🧠 Loading NLP Model (KeyBERT) for dynamic entity extraction...")
kw_model = KeyBERT(model='all-MiniLM-L6-v2')


def _build_keyword_pattern(keywords):
    """
    Compiles a keyword list into a single word-boundary regex.

    Built once at import rather than per call: the previous implementation ran a
    substring test per keyword per post, which both cost N scans and produced false
    positives ('ai' inside "IsaacLab", 'ml' inside "html").
    """
    alternatives = '|'.join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    return re.compile(r'\b(?:' + alternatives + r')\b', re.IGNORECASE)


STRONG_PATTERN = _build_keyword_pattern(AI_STRONG_KEYWORDS)
WEAK_PATTERN = _build_keyword_pattern(AI_WEAK_KEYWORDS)
JUNK_PATTERN = re.compile('|'.join(re.escape(p) for p in JUNK_PATTERNS), re.IGNORECASE)


class BaseCollector(ABC):
    def __init__(self, platform_name):
        self.platform_name = platform_name
        self.stats_config = {
            'min_stdev': 1.0,
            'damping_factor': 1.0,
            'sigmoid_shift': 0.5,
            'log_base': 10
        }
        # Content hashes seen during the current run, so byte-identical posts published
        # under different external_ids are not stored several times.
        # Per-collector length floor. GitHub repo descriptions are short but curated
        # and dense, so that collector lowers this rather than discarding the repo.
        self.min_content_chars = MIN_CONTENT_CHARS
        self._seen_hashes = set()
        # Tally of why posts were rejected. Purely diagnostic; read by the dry-run harness.
        self.reject_reasons = Counter()

    @staticmethod
    def clean_text(text):
        """Sanitizes text and converts HTML entities back to plain text."""
        if not text: return ""
        # 1. Convert entities like &amp; to &
        clean = html.unescape(text)
        # 2. Remove HTML tags
        clean = re.sub(r'<[^>]+>', ' ', clean)
        # 3. Remove URLs
        clean = re.sub(r'http[s]?://\S+', '', clean)
        clean = re.sub(r'www\.\S+', '', clean)
        # 4. Normalize spaces
        clean = re.sub(r'\s+', ' ', clean)
        return clean.strip()

    @staticmethod
    def to_iso8601(value):
        """
        Normalizes a publication timestamp to an ISO8601 UTC string.

        Platforms disagree on format: Hacker News' Firebase API returns a Unix epoch int
        while Dev.to, GitHub and Mastodon return ISO strings. Storing both made
        published_at unsortable and unparseable downstream.
        """
        if value in (None, ''): return ''
        # Unix epoch, as an int or a numeric string
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            try:
                return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                return ''
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            return str(value)

    @staticmethod
    def is_ai_relevant(text, title=''):
        """
        Word-boundary relevance test with two keyword tiers.

        A strong term ('llm', 'pytorch') is accepted anywhere. Weak terms ('ai', 'agent',
        'vector') are ambiguous in isolation, so they are accepted only in the title --
        which is short and deliberate -- or when two DISTINCT weak terms co-occur in the
        body. Substring matching previously let ~19% of stored posts through on hits like
        'ai' inside "IsaacLab".
        """
        if not text: return False

        if STRONG_PATTERN.search(text):
            return True
        if title and WEAK_PATTERN.search(title):
            return True

        distinct_weak = {m.group(0).lower() for m in WEAK_PATTERN.finditer(text)}
        return len(distinct_weak) >= 2

    @staticmethod
    def extract_keywords(text):
        if not text: return []
        try:
            # MMR (Maximal Marginal Relevance) suppresses near-duplicate phrases. Without
            # it a single repo yielded "xla compiler", "xla compile", "xla accelerated",
            # "project xla" and "xla project" -- five variants of one idea.
            extracted = kw_model.extract_keywords(
                text,
                keyphrase_ngram_range=(1, 2),
                stop_words='english',
                top_n=8,
                use_mmr=True,
                diversity=0.6,
            )
            keywords = [phrase.lower() for phrase, score in extracted if score > 0.35]
            return keywords[:5]
        except Exception as e:
            print(f"Extraction error: {e}")
            return []

    @staticmethod
    def analyze_sentiment(text):
        if not text or not isinstance(text, str): return 0.0
        try:
            return TextBlob(text).sentiment.polarity
        except:
            return 0.0

    def _structural_reject(self, text):
        """
        Cheap structural checks run before any model touches the text.

        Returns a rejection reason, or None if the text looks like real prose.
        """
        if JUNK_PATTERN.search(text[:400]):
            return 'junk_page'          # Bot wall or paywall returned with HTTP 200
        if len(text) < self.min_content_chars:
            return 'too_short'
        tokens = text.split()
        if len(tokens) < max(8, MIN_WORD_COUNT * self.min_content_chars // MIN_CONTENT_CHARS):
            return 'too_few_words'
        if sum(c.isalpha() or c.isspace() for c in text) / len(text) < MIN_ALPHA_RATIO:
            return 'low_alpha_ratio'    # Binary, base64 or navigation junk
        if sum(t.startswith('#') for t in tokens) / len(tokens) > MAX_HASHTAG_RATIO:
            return 'hashtag_soup'
        # Measured over a FIXED window, not the whole text: vocabulary richness falls
        # naturally as documents grow, so a global ratio rejects long articles rather
        # than spam. Genuine repetition shows up within the first couple of hundred words.
        words = [t.lower() for t in tokens[:REPETITION_WINDOW] if t.isalpha()]
        if len(words) >= 20 and len(set(words)) / len(words) < MIN_UNIQUE_TOKEN_RATIO:
            return 'repetitive'
        return None

    def is_quality_content(self, post):
        """
        Final gatekeeper: structural, relevance, language and duplication checks.

        Ordered cheapest-first so the expensive stages (langdetect, then KeyBERT) run on
        the smallest possible set of candidates.
        """
        post['title'] = self.clean_text(post.get('title', ''))
        post['content'] = self.clean_text(post.get('content', ''))
        full_text = f"{post['title']} {post['content']}"

        # Structural checks run on the body alone: Mastodon derives its title from its
        # own content, so measuring title+content would let a short toot clear the
        # length floor twice over.
        reason = self._structural_reject(post['content'])
        if reason:
            self.reject_reasons[reason] += 1
            return False

        if not self.is_ai_relevant(full_text, post['title']):
            self.reject_reasons['not_ai_relevant'] += 1
            return False

        # Drop posts whose text we have already accepted this run (e.g. the same toot
        # reposted under three different ids).
        content_hash = hashlib.sha256(
            re.sub(r'\W+', '', post['content'].lower()).encode('utf-8')
        ).hexdigest()
        if content_hash in self._seen_hashes:
            self.reject_reasons['duplicate'] += 1
            return False

        # Enhanced language detection
        text_to_check = full_text[:500]
        if len(text_to_check) > 25:
            try:
                langs = detect_langs(text_to_check)
                if langs[0].lang != 'en' or langs[0].prob < LANG_MIN_PROB:
                    self.reject_reasons['not_english'] += 1
                    return False
            except LangDetectException:
                self.reject_reasons['lang_undetectable'] += 1
                return False

        post['keywords'] = self.extract_keywords(full_text)
        if not post['keywords']:
            self.reject_reasons['no_keywords'] += 1
            return False

        # Recompute on sanitized text: collectors call analyze_sentiment() on the raw
        # payload, so scores were previously derived from HTML and markdown noise.
        post['sentiment'] = self.analyze_sentiment(full_text)
        post['published_at'] = self.to_iso8601(post.get('published_at'))

        self._seen_hashes.add(content_hash)
        return True

    def recalculate_platform_stats(self):
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, raw_score FROM unified_posts WHERE source_platform = ?', (self.platform_name,))
            rows = cursor.fetchall()
            if not rows: return
            processed_scores = [math.log10(r[1] + 1) if r[1] > 0 else 0 for r in rows]
            mean, std_dev = np.mean(processed_scores), np.std(processed_scores) or 1.0
            damp, shift = self.stats_config.get('damping_factor', 1.0), self.stats_config.get('sigmoid_shift', 0.5)
            for row, scaled_score in zip(rows, processed_scores):
                z_score = ((scaled_score - mean) / (std_dev * damp)) + shift
                trend_score = (1 / (1 + math.exp(-z_score))) * 100
                cursor.execute('UPDATE unified_posts SET trend_score = ? WHERE id = ?', (trend_score, row[0]))
            conn.commit()

    @abstractmethod
    async def collect(self, client):
        pass
