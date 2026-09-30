"""
The collection engine, callable from the dashboard.

This used to live in main.py as an hourly scheduler that had to be started in a
second terminal before the dashboard had anything to show. The dashboard is now the
only entry point: when the user searches for a topic, ui/app.py calls `scan()` and
this module plans the search, runs all four collectors concurrently, scores and
stores the results, and returns a summary for the page to report.

Nothing here imports Streamlit. Progress is reported through an optional callback
so the UI can render a live loading state, and the module stays usable from a
plain script or a test.
"""

import asyncio
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import httpx

import config
import gemini_client
from topics import TopicProfile
from database.manager import TrendManager

# Longest topic accepted from the search box. The topic is interpolated into the
# planning prompt, and nobody's genuine interest needs more than this.
MAX_TOPIC_CHARS = 80

NO_RESULTS_MESSAGE = "No relevant trends found for this topic."

# ==========================================================================
# 🧭 AGENTIC INGESTION LAYER
# ==========================================================================

# The planning prompt. Written as an explicit schema with per-field constraints
# rather than a request for "search terms", because each of the four platforms
# accepts a different query shape and a generic answer is unusable at three of
# them: GitHub repository search has no boolean OR and only matches real topic
# slugs, Dev.to filters on a single tag per request, and Mastodon tag timelines
# reject anything that is not a bare alphanumeric hashtag. Asking for the four
# shapes separately is what makes the output directly executable.
TOPIC_PLAN_PROMPT = """You are the search strategist for a technology market-intelligence
pipeline. It ingests posts from GitHub, Hacker News, Dev.to and Mastodon, then looks
for a topic emerging across all four at once.

TARGET TOPIC: "{topic}"

Produce the exact query plan for ONE collection cycle. Reply with a single JSON
object and nothing else, using these keys:

{{
  "recognised":      true or false,
  "github_topics":   [3-6 GitHub *topic slugs*, best first],
  "hn_queries":      [3-5 short free-text search phrases for the Hacker News index],
  "devto_tags":      [2-4 Dev.to tags],
  "mastodon_tags":   [3-5 Mastodon hashtags, the most-used one first],
  "strong_keywords": [12-25 unambiguous terms],
  "weak_keywords":   [6-14 ambiguous terms],
  "rationale":       "one sentence on how you scoped the topic"
}}

If the target topic is not a real, recognisable subject -- random letters, keyboard
mashing, or words with no meaning -- reply {{"recognised": false}} and nothing else.
Do NOT substitute a different or more general topic: an empty result is correct.

Field rules -- a value breaking these is discarded and replaced by the literal search
words, so precision here directly determines how much of the plan survives:

* github_topics: lowercase, hyphens allowed, no spaces, and they must be topics
  people genuinely tag repositories with ("ai-agents", "llm", "rag"). Not phrases.
* hn_queries: plain search phrases, under 60 characters, no boolean operators and
  no quotes. These are matched against story titles and text.
* devto_tags / mastodon_tags: single lowercase alphanumeric tokens, no hyphens and
  no '#' ("machinelearning", not "machine-learning" and not "#machine learning").
* strong_keywords: terms whose presence ANYWHERE in a post is on its own sufficient
  proof the post is about the target topic. Prefer named technologies, products,
  libraries, models, protocols and techniques ("langgraph", "tool calling", "mcp").
  A term that appears in unrelated software writing does NOT belong here.
* weak_keywords: terms that suggest the topic but are ambiguous alone, because they
  are ordinary English or ordinary engineering vocabulary ("agent", "workflow",
  "orchestration", "memory"). These are only accepted in a post's title, or when two
  different ones co-occur in its body, so put genuinely ambiguous terms here rather
  than padding the strong list with them.
* All keywords lowercase, 2-40 characters, letters/digits/spaces/hyphens/dots only.
* Cover the topic's vocabulary as actually written by practitioners, including
  abbreviations and the names of the dominant tools -- recall matters more than
  elegance, because a second filter downstream removes low-quality text anyway.
"""


class DynamicTopicManager:
    """
    Turns a high-level topic into an executable, run-specific search plan.

    This is the "agentic" step: instead of shipping a fixed AI vocabulary, the
    engine is told *what to look for* in plain language and works out *how to
    look for it* -- which per-platform queries to issue, and which terms count as
    evidence of relevance once results return.

    Failure is a first-class path, not an exception. `resolve()` always returns a
    usable TopicProfile: a generated one when the model answers, and one built from
    the user's own search words when the SDK is missing, no key is set, the call
    times out or hits a quota, or the response does not survive validation. That
    fallback is silent: it is an expected path, not an error. `profile.source`
    records which happened, and the dashboard shows it.

    Plans are cached per manager, and `scan()` keeps one manager per topic for the
    process lifetime, so re-scanning a topic reuses its plan instead of spending an
    API call on a near-identical answer. `refresh()` forces a new one.
    """

    def __init__(self, target_topic=None, timeout=None):
        self.target_topic = (target_topic or config.TARGET_TOPIC).strip()
        self.timeout = timeout or config.TOPIC_PLAN_TIMEOUT
        self._cache = {}

    # --- Public API ---------------------------------------------------------

    def resolve(self, refresh=False):
        """
        Returns the TopicProfile for this manager's topic. Never raises.

        Args:
            refresh (bool): Re-plan even if this topic is already cached.
        """
        if not refresh and self.target_topic in self._cache:
            return self._cache[self.target_topic]

        profile = self._plan() or self._fallback()
        # Only a genuine plan is cached. A fallback caused by a transient failure
        # (timeout, rate limit) should not pin the topic to keyword search for the
        # rest of the session -- the next scan tries the planner again.
        if profile.source == "gemini":
            self._cache[self.target_topic] = profile
        return profile

    def refresh(self):
        """Discards the cached plan and asks for a new one."""
        return self.resolve(refresh=True)

    async def resolve_async(self):
        """
        Awaitable `resolve()`, offloaded to the default executor.

        The planner is one blocking HTTP call at the head of an otherwise fully
        concurrent cycle; running it in a thread keeps the event loop free and
        means the timeout is enforced without stalling the loop itself.
        """
        return await asyncio.get_running_loop().run_in_executor(None, self.resolve)

    # --- Internals ----------------------------------------------------------

    def _plan(self):
        """Asks Gemini for a plan. Returns a TopicProfile, or None to fall back."""
        # Failure here is silent by design -- no key, no SDK, a quota limit or a
        # timeout all mean the same thing to the user: we search for the words
        # they typed. It is a normal path, not something to warn about mid-scan.
        ok, _ = gemini_client.status()
        if not ok:
            return None

        payload, error = gemini_client.generate_json(
            TOPIC_PLAN_PROMPT.format(topic=self.target_topic),
            # Low but non-zero: a query plan should be reproducible between
            # scans, not creative.
            temperature=0.25,
            timeout=self.timeout,
        )
        if error or not payload:
            return None

        profile = TopicProfile.from_payload(payload, self.target_topic, source="gemini")
        profile.generated_at = datetime.now(timezone.utc).isoformat()
        return profile

    def _fallback(self):
        """
        A plan built from the search words themselves, tagged so the UI can say so.

        Not the curated config.py plan: that is an AI vocabulary, and using it for
        an arbitrary user search would return AI posts under someone else's topic.
        """
        profile = TopicProfile.from_search_terms(self.target_topic)
        profile.generated_at = datetime.now(timezone.utc).isoformat()
        return profile


# ==========================================================================
# 🚀 SCAN ORCHESTRATION
# ==========================================================================

@dataclass
class ScanResult:
    """What one scan did, in the terms the dashboard reports to the user."""
    topic: str
    plan_source: str = ""
    fetched: int = 0
    saved: int = 0
    per_platform: dict = field(default_factory=dict)
    # Per-platform volume before the storage cap; see BaseCollector.volume_report().
    volume: dict = field(default_factory=dict)
    duration_s: float = 0.0
    replaced_previous_topic: bool = False
    # ok | invalid | no_results | busy | failed. The UI picks its message on this,
    # so "nothing matched" is presented as an answer rather than as an error.
    status: str = "ok"
    error: str = ""

    def to_dict(self):
        return asdict(self)


def normalise_topic(text):
    """Collapses whitespace and caps length. Returns '' for an unusable topic."""
    return re.sub(r'\s+', ' ', str(text or '')).strip()[:MAX_TOPIC_CHARS].strip()


# What a search may contain: English letters, digits, spaces and the punctuation
# found in technology names ("c++", "node.js", "c#", "ci/cd", "r&d", "gpt-5").
# Every source indexed here is English, and the relevance filter matches on English
# word boundaries, so anything else (Hebrew, Cyrillic, CJK, emoji) cannot match.
_ENGLISH_TOPIC_RE = re.compile(r"^[A-Za-z0-9 .,+#&'/()\-:]+$")

ENGLISH_ONLY_MESSAGE = "Please enter English terms only."


def validate_topic(text):
    """
    Checks a search before any work starts. Returns (topic, error_message).

    Run by the dashboard before the scan is even queued, and again inside scan()
    so no other caller can bypass it.
    """
    topic = normalise_topic(text)
    if not topic:
        return "", "Type a topic to scan for."
    if not _ENGLISH_TOPIC_RE.match(topic):
        return "", ENGLISH_ONLY_MESSAGE
    if not re.search(r'[A-Za-z]', topic):
        return "", "Please include at least one word."
    return topic, ""


def current_topic():
    """The topic the stored corpus was collected under, or None before any scan."""
    profile = TopicProfile.load()
    return profile.target_topic if profile else None


# One manager per topic for the process lifetime, so a cached plan survives
# Streamlit reruns and repeated scans of the same topic.
_MANAGERS = {}
_MANAGERS_LOCK = threading.Lock()

# One scan at a time per process. Every browser tab shares the same SQLite file,
# and two interleaved scans would each clear and re-score the other's rows.
_SCAN_LOCK = threading.Lock()


def _manager_for(topic):
    key = topic.lower()
    with _MANAGERS_LOCK:
        if key not in _MANAGERS:
            _MANAGERS[key] = DynamicTopicManager(topic)
        return _MANAGERS[key]


def is_scanning():
    """True while any session in this process is running a scan."""
    return _SCAN_LOCK.locked()


def _emit(progress, stage, **info):
    """
    Reports a stage to the UI. A broken callback must never cost the scan, so
    ordinary errors are swallowed -- but only `Exception`: Streamlit stops a script
    by raising a BaseException through whatever it is executing, and swallowing
    that would leave a scan running that the user has already navigated away from.
    """
    if progress is None:
        return
    try:
        progress(stage, info)
    except Exception as exc:                      # noqa: BLE001
        print(f"   ⚠️  Progress callback failed at '{stage}': {exc}")


async def run_cycle(topic, progress=None):
    """
    Executes one collection cycle for `topic` and returns a ScanResult.

    Stages reported to `progress(stage, info)`, in order:
        loading       -> collectors (and the KeyBERT model) are being imported
        planning      -> the search plan is being resolved
        planned       -> info['profile'] is the TopicProfile in use
        collecting    -> all platforms have been dispatched
        platform_done -> info['platform'], info['count'] (once per platform)
        scoring       -> results are being stored, normalised and pruned

    All four collectors run concurrently with asyncio.gather, so the scan takes as
    long as the slowest platform rather than the sum of all four.

    A new topic REPLACES the stored corpus rather than adding to it: the dashboard
    shows one topic at a time. The swap only happens once the new results are in
    hand, so a scan that finds nothing leaves the previous dashboard intact.
    """
    started = time.monotonic()
    result = ScanResult(topic=topic)

    # Imported here, not at module scope: collectors/base.py loads the KeyBERT
    # model on import, which would otherwise add several seconds to every
    # dashboard start before the user has asked for anything.
    _emit(progress, "loading")
    from collectors.github import GitHubCollector
    from collectors.hacker_news import HackerNewsCollector
    from collectors.mastodon import MastodonCollector
    from collectors.devto import DevToCollector

    print("\n" + "=" * 80)
    print(f"🕒 SCAN STARTING | TOPIC: {topic} | "
          f"TIME: {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 80)

    db_manager = TrendManager()
    collectors = [GitHubCollector(), HackerNewsCollector(),
                  MastodonCollector(), DevToCollector()]

    # 0. Resolve the search plan and distribute it to every collector.
    _emit(progress, "planning")
    profile = await _manager_for(topic).resolve_async()
    for collector in collectors:
        collector.apply_topic_profile(profile)
    result.plan_source = profile.source
    print(f">>> 🧭 Active plan: {profile.summary()}")
    _emit(progress, "planned", profile=profile)

    # Nothing searchable (the planner did not recognise the topic, or it has no
    # usable words): stop before a single request is made.
    if profile.is_empty():
        result.status = "no_results"
        result.error = NO_RESULTS_MESSAGE
        result.duration_s = time.monotonic() - started
        return result

    # 1. Ingest from all platforms concurrently.
    async def collect_one(collector, client):
        try:
            posts = await collector.collect(client)
        except Exception as exc:                  # noqa: BLE001
            # One platform being down should not cost the other three.
            print(f"   ❌ {collector.platform_name} collector failed: {exc}")
            posts = []
        report = collector.volume_report(len(posts))
        _emit(progress, "platform_done", platform=collector.platform_name,
              count=len(posts), volume=report)
        return collector.platform_name, posts, report

    _emit(progress, "collecting")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                 headers={'User-Agent': 'TrendAnalyzer/6.0'}) as client:
        platform_results = await asyncio.gather(
            *(collect_one(c, client) for c in collectors)
        )

    all_posts = []
    for platform, posts, report in platform_results:
        result.per_platform[platform] = len(posts)
        result.volume[platform] = report
        all_posts.extend(posts)
    result.fetched = len(all_posts)

    if not all_posts:
        # Abort before touching the database: the previous topic's dashboard stays
        # exactly as it was, and no empty charts are rendered for this one.
        result.status = "no_results"
        result.error = NO_RESULTS_MESSAGE
        result.duration_s = time.monotonic() - started
        return result

    # 2. Store. A new topic replaces the corpus; a repeat scan adds to it.
    _emit(progress, "scoring")
    previous = current_topic()
    result.replaced_previous_topic = (previous or '').strip().lower() != topic.lower()
    if result.replaced_previous_topic:
        result.saved = db_manager.replace_posts(all_posts)
    else:
        result.saved = db_manager.save_posts(all_posts)
    print(f">>> 💾 Saved {result.saved} new unique items to the database.")

    # 3. Per-platform normalisation, then Top-K retention.
    for collector in collectors:
        collector.recalculate_platform_stats()
    db_manager.prune_database()
    db_manager.get_db_stats()
    # Saved with the corpus, never on a no-results path, so the volume on screen
    # always belongs to the posts on screen.
    db_manager.save_volume(topic, list(result.volume.values()))
    for report in result.volume.values():
        print(f"    📈 {report['platform']:<12} ≈{report['estimated_matches']} matching "
              f"({report['searched_total']} found, {report['overlap']:.0%} unique, "
              f"{report['acceptance_rate']:.0%} passed the gate)")

    # Persisted last, so the dashboard's topic label only changes once the
    # corpus underneath it actually belongs to the new topic.
    profile.save()

    result.duration_s = time.monotonic() - started
    print(f">>> ✅ Scan complete in {result.duration_s:.0f}s. "
          f"Fetched {result.fetched} items.")
    return result


def _run_coroutine(coro):
    """
    Runs a coroutine to completion from synchronous code.

    Streamlit executes the script in a plain thread with no event loop, so the
    normal case is a direct `asyncio.run` in that thread -- which also means the
    progress callback fires on the script thread and can update the page live.
    If a loop IS already running here (some hosts install one), `asyncio.run`
    would refuse, so the coroutine gets a private loop on a worker thread instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def scan(topic, progress=None):
    """
    Synchronous entry point for the dashboard: plan, collect, score and store.

    Never raises for ordinary failures; they come back as `ScanResult.error` in a
    sentence fit to show the user.
    """
    topic, invalid = validate_topic(topic)
    if invalid:
        return ScanResult(topic="", status="invalid", error=invalid)

    if not _SCAN_LOCK.acquire(blocking=False):
        return ScanResult(topic=topic, status="busy",
                          error="Another scan is already running. "
                                "Give it a minute and try again.")
    try:
        return _run_coroutine(run_cycle(topic, progress))
    except Exception as exc:                      # noqa: BLE001
        print(f"❌ Critical error during scan: {exc}")
        return ScanResult(topic=topic, status="failed",
                          error=f"The scan stopped unexpectedly: {exc}")
    finally:
        _SCAN_LOCK.release()
