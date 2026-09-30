from abc import ABC, abstractmethod
from datetime import datetime, timezone
from collections import Counter
import asyncio
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
    TARGET_POSTS_PER_PLATFORM, COLLECTION_BATCH_SIZE,
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


# Fallback tiers, compiled from the curated lists in config.py. A collector that
# has been handed a TopicProfile compiles its own pair in apply_topic_profile() and
# these are never consulted; they are what the Gatekeeper runs on when the agentic
# planner is unavailable, which keeps relevance filtering identical to the previous
# hardcoded behaviour rather than absent.
DEFAULT_STRONG_PATTERN = _build_keyword_pattern(AI_STRONG_KEYWORDS)
DEFAULT_WEAK_PATTERN = _build_keyword_pattern(AI_WEAK_KEYWORDS)

# Retained under their original names: external callers and the dry-run harness
# import these.
STRONG_PATTERN = DEFAULT_STRONG_PATTERN
WEAK_PATTERN = DEFAULT_WEAK_PATTERN

# A pattern that can never match, used for a plan tier that came back empty.
NEVER_MATCH = re.compile(r'(?!)')

JUNK_PATTERN = re.compile('|'.join(re.escape(p) for p in JUNK_PATTERNS), re.IGNORECASE)

# Sentence terminator followed by whitespace. Used to find a clean cut point; the
# lookbehind keeps single-letter initials and decimals ("v1.5", "U.S.") from counting.
_SENTENCE_END_RE = re.compile(r'(?<=[a-z0-9\)\]"\'])[.!?][\s]')

# Never give back less than this fraction of the budget when backing up to a sentence
# boundary. A document with one 9k-character paragraph has no terminator to find, and
# rewinding to the last one would throw away most of the text.
_MIN_BOUNDARY_RATIO = 0.75


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
        # Relevance vocabulary for THIS run. Defaults to the curated config tiers so
        # a collector constructed without a profile behaves exactly as before;
        # apply_topic_profile() swaps in the planner's tiers in memory, with no
        # config edit and nothing written to disk.
        self.topic_profile = None
        self._strong_pattern = DEFAULT_STRONG_PATTERN
        self._weak_pattern = DEFAULT_WEAK_PATTERN
        self._seen_hashes = set()
        # Tally of why posts were rejected. Purely diagnostic; read by the dry-run harness.
        self.reject_reasons = Counter()
        # Volume bookkeeping for volume_report(). The collectors stop at
        # TARGET_POSTS_PER_PLATFORM stored posts, so the stored count says nothing
        # about how much matching content actually exists; these record what the
        # platform reported and how the gate treated the posts it did check.
        self.search_totals = []        # platform-reported match counts, one per query
        self.search_capped = False     # a total hit the platform's reporting ceiling
        self.fetched_rows = 0          # candidate rows returned across all queries
        self.unique_candidates = 0     # ...after de-duplicating between queries
        self.overlap_factor = None     # override when overlap is measured another way
        self.gated = 0                 # candidates run through the quality gate
        self.gate_accepted = 0         # ...and how many passed, before the target cap

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
    def truncate_at_boundary(text, limit):
        """
        Cuts `text` to at most `limit` characters without slicing a sentence in half.

        Plain slicing left every deep-fetched body ending mid-word ("outputs that can be
        programmatically v"), which reads as damaged content in the UI and feeds KeyBERT
        a broken final phrase. We rewind to the last sentence terminator inside the
        budget, or to the last word break if the tail holds no terminator, and append
        NOTHING -- an added "..." is indistinguishable from a real ellipsis in the source.
        """
        if not text:
            return ""
        text = text.strip()
        if limit <= 0 or len(text) <= limit:
            return text

        head = text[:limit]
        floor = int(limit * _MIN_BOUNDARY_RATIO)

        # Last full sentence that ends inside the budget.
        matches = list(_SENTENCE_END_RE.finditer(head))
        if matches and matches[-1].start() + 1 >= floor:
            return head[:matches[-1].start() + 1].strip()

        # No usable sentence end: fall back to the last whole word.
        space = head.rfind(' ')
        if space >= floor:
            return head[:space].strip()
        return head.strip()

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

    def apply_topic_profile(self, profile):
        """
        Points this collector at a run-specific topic plan.

        The profile's two keyword tiers are compiled into word-boundary patterns
        and held ON THE INSTANCE -- the module-level defaults are left untouched, so
        two collectors could run against different topics in one process and a
        failed plan degrades per collector rather than globally.

        Only the VOCABULARY is swapped. Tier semantics (strong anywhere, weak in a
        title or twice-distinct in a body) and every structural threshold --
        MIN_ALPHA_RATIO, MAX_HASHTAG_RATIO, MIN_UNIQUE_TOKEN_RATIO, the language
        floor, the duplicate hash -- are unchanged and still read from config.py.
        Those gates reject malformed text, which has nothing to do with the topic;
        letting a generated plan weaken them would hand the model a lever over
        data quality it has no basis to pull.

        A profile with an empty tier is ignored for that tier rather than applied,
        because an empty pattern would match nothing and silently reject the whole
        corpus.
        """
        if profile is None:
            return
        self.topic_profile = profile
        # An empty tier matches NOTHING rather than falling back to the curated AI
        # vocabulary: keeping the defaults here let an unrelated search accept
        # generic AI posts, which is how nonsense queries came back with a feed.
        strong = getattr(profile, 'strong_keywords', None)
        weak = getattr(profile, 'weak_keywords', None)
        self._strong_pattern = _build_keyword_pattern(strong) if strong else NEVER_MATCH
        self._weak_pattern = _build_keyword_pattern(weak) if weak else NEVER_MATCH

    def planned(self, field, default):
        """
        One per-platform query list from the run's plan.

        The curated `default` is used only when NO plan was applied (a bare
        collector, as in the dry-run harness). Once a plan is in place an empty
        field means "nothing to search for on this platform" and yields [] -- never
        the curated AI queries, which would return an unrelated feed.
        """
        if self.topic_profile is None:
            return list(default)
        return list(getattr(self.topic_profile, field, None) or [])

    def is_topic_relevant(self, text, title=''):
        """
        Word-boundary relevance test with two keyword tiers.

        A strong term ('llm', 'pytorch') is accepted anywhere. Weak terms ('ai', 'agent',
        'vector') are ambiguous in isolation, so they are accepted only in the title --
        which is short and deliberate -- or when two DISTINCT weak terms co-occur in the
        body. Substring matching previously let ~19% of stored posts through on hits like
        'ai' inside "IsaacLab".

        The tiers come from the instance, so the test follows whatever topic the run
        was pointed at; the acceptance RULE above is fixed.
        """
        if not text: return False

        if self._strong_pattern.search(text):
            return True
        if title and self._weak_pattern.search(title):
            return True

        distinct_weak = {m.group(0).lower() for m in self._weak_pattern.finditer(text)}
        return len(distinct_weak) >= 2

    def is_ai_relevant(self, text, title=''):
        """Former name of is_topic_relevant(), kept so existing callers work."""
        return self.is_topic_relevant(text, title)

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

        if not self.is_topic_relevant(full_text, post['title']):
            self.reject_reasons['not_topic_relevant'] += 1
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

    async def collect_until_target(self, candidates, process, target=None, batch_size=None):
        """
        Runs `process` over `candidates` in concurrent waves until `target` posts pass.

        The collectors used to slice their candidate pool to the target size and hand the
        whole slice to asyncio.gather() in one shot, so every gatekeeper rejection came
        straight off the final count -- 50 candidates minus 40 rejections left GitHub
        storing 10 posts. Here the pool is deliberately oversampled and consumed lazily:
        each wave is sized from the acceptance rate measured so far, and the walk stops
        the moment the target is met, so a healthy platform still pays for only one wave.

        Args:
            candidates (list): Oversampled raw items, best-first.
            process (callable): async fn(item) -> post dict, or None if rejected.
            target (int): Accepted posts wanted. Defaults to TARGET_POSTS_PER_PLATFORM.
            batch_size (int): Size of the first wave. Defaults to COLLECTION_BATCH_SIZE.

        Returns:
            list: Accepted posts, capped at `target`.
        """
        target = target or TARGET_POSTS_PER_PLATFORM
        wave_size = batch_size or COLLECTION_BATCH_SIZE
        accepted, cursor = [], 0

        while cursor < len(candidates) and len(accepted) < target:
            batch = candidates[cursor:cursor + max(1, wave_size)]
            cursor += len(batch)

            results = await asyncio.gather(
                *[process(item) for item in batch], return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    self.reject_reasons['processing_error'] += 1
                elif result is not None:
                    accepted.append(result)

            if len(accepted) >= target:
                break

            # Size the next wave from the observed yield, with a pessimistic floor so a
            # first wave that accepted nothing still requests a bounded batch rather
            # than the entire remaining pool.
            rate = max(len(accepted) / cursor, 0.2)
            wave_size = min(
                len(candidates) - cursor,
                int((target - len(accepted)) / rate) + 2,
            )

        # Measured BEFORE the cap: the acceptance rate must describe the gate, not
        # the target.
        self.gated += cursor
        self.gate_accepted += len(accepted)

        # A final wave can overshoot; the caller contracted for `target`.
        accepted = accepted[:target]

        if len(accepted) < target:
            print(f"   ⚠️  {self.platform_name}: {len(accepted)}/{target} posts cleared the "
                  f"gate from {cursor} candidates. Rejections: {dict(self.reject_reasons)}")
        else:
            print(f"   ✅ {self.platform_name}: {len(accepted)} posts from {cursor} candidates.")

        return accepted

    def record_candidates(self, batches, key):
        """
        Notes how many candidate rows the searches returned and how many were unique.

        The ratio is the overlap correction for search_totals: the queries of one
        plan overlap heavily ('ai' and 'ai-agents' tag many of the same repositories),
        so summing their totals would count those items once per query.
        """
        rows = [item for batch in batches for item in batch]
        self.fetched_rows += len(rows)
        self.unique_candidates += len({item.get(key) for item in rows})

    def volume_report(self, stored):
        """
        Estimated number of posts on this platform that match the search AND would
        pass the quality gate -- the volume before the TARGET_POSTS_PER_PLATFORM cut.

            estimate = platform-reported total
                       x overlap correction  (unique / fetched rows across queries)
                       x gate acceptance     (passed / checked, on the posts we checked)

        Without a platform total (none reported, or the requests failed) the unique
        candidate count stands in, which is a lower bound. The estimate is never
        below the number of posts actually stored.
        """
        if self.overlap_factor is not None:
            overlap = self.overlap_factor
        elif self.fetched_rows:
            overlap = self.unique_candidates / self.fetched_rows
        else:
            overlap = 1.0
        acceptance = self.gate_accepted / self.gated if self.gated else 0.0

        if self.search_totals:
            searched = int(sum(self.search_totals))
            estimate = searched * overlap * acceptance
            source = "platform"
        else:
            searched = self.unique_candidates
            estimate = searched * acceptance
            source = "candidates"

        return {
            'platform': self.platform_name,
            'searched_total': searched,
            'overlap': round(overlap, 4),
            'acceptance_rate': round(acceptance, 4),
            'estimated_matches': max(int(round(estimate)), int(stored)),
            'stored': int(stored),
            'capped': bool(self.search_capped),
            'source': source,
        }

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
