"""
Central configuration for TrendAnalyzer Pro.

Single source of truth for every tunable constant of the pipeline, grouped by stage:

1. Paths             -- the SQLite corpus and the persisted topic plan (in data/).
2. Platforms & NLP   -- canonical platform order and the shared embedding model.
3. Topic planning    -- the default topic and the curated fallback query plan.
4. Relevance tiers   -- the fallback two-tier keyword vocabulary of the quality gate.
5. Quality gate      -- structural thresholds that reject malformed or low-value text.
6. Sampling          -- per-platform targets, oversampling and wave sizing.
7. Rate limiting     -- per-host concurrency caps and the GitHub quota reserve.
8. Background worker -- APScheduler cadence, seed discovery and data retention.
9. Momentum & mood   -- when a topic is strongly trending; affinity labels.

Side-effect free (environment reads only), so it is safe to import from any layer.
"""

import os

# ---------------------------------------------------------------------------
# 1. Paths
# ---------------------------------------------------------------------------
# Anchored to this file, not the CWD, so every entry point resolves the same paths.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "trends_project.db")
PROFILE_PATH = os.path.join(DATA_DIR, "topic_profile.json")

# ---------------------------------------------------------------------------
# 2. Platforms & NLP
# ---------------------------------------------------------------------------
# Canonical platform order, used for balanced sampling and consistent display.
PLATFORMS = ("GitHub", "Hacker News", "Dev.to", "Mastodon")

# One encoder for KeyBERT (ingestion) and the semantic graph (analysis).
EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
# CPU on purpose: Apple's MPS backend is not thread-safe, and the interactive and
# background scans encode on separate threads. MiniLM is cheap on CPU.
EMBED_DEVICE = "cpu"

# ---------------------------------------------------------------------------
# 3. Topic planning
# ---------------------------------------------------------------------------
# engine.DynamicTopicManager turns any topic into per-platform queries and a
# relevance vocabulary.
TARGET_TOPIC = os.environ.get("TARGET_TOPIC", "AI and machine learning")

# Per-platform fallback plans. Each search API accepts a different query shape:
#   GitHub      topic slugs   (repository search has no boolean OR)
#   Hacker News free text     (Algolia full-text index)
#   Dev.to      single tags   (one tag per /articles request)
#   Mastodon    hashtags      (bare alphanumeric tags only)
# Ordered best-first: the GitHub collector applies stricter star floors to the head.
DEFAULT_GITHUB_TOPICS = [
    'llm', 'machine-learning', 'ai-agents', 'generative-ai',
    'deep-learning', 'nlp', 'rag',
]
DEFAULT_HN_QUERIES = ["LLM", "AI model", "GPT", "machine learning", "AI agents"]
DEFAULT_DEVTO_TAGS = ["ai", "machinelearning", "llm"]
DEFAULT_MASTODON_TAGS = ["ai", "llm", "machinelearning", "genai", "generativeai"]

# ---------------------------------------------------------------------------
# 4. Relevance tiers (fallback vocabulary of the quality gate)
# ---------------------------------------------------------------------------
# Two-tier word-boundary matching. Short, ambiguous tokens ('ai', 'ml') need more
# evidence than specific ones. A generated plan replaces both lists per run.

# Strong tier: a single occurrence anywhere accepts a post.
DEFAULT_STRONG_KEYWORDS = [
    'llm', 'gpt', 'machine learning', 'deep learning', 'neural network',
    'neural networks', 'transformer', 'transformers', 'pytorch', 'tensorflow',
    'openai', 'anthropic', 'diffusion model', 'stable diffusion', 'rag',
    'fine-tuning', 'fine tuning', 'huggingface', 'hugging face', 'deepseek',
    'embeddings', 'generative ai', 'agentic', 'inference', 'gemini', 'llama',
]

# Weak tier: accepted only in a title, or when two distinct weak terms co-occur.
DEFAULT_WEAK_KEYWORDS = [
    'ai', 'ml', 'nlp', 'agent', 'agents', 'automation', 'vector',
    'data science', 'claude', 'neural', 'model', 'models', 'training',
]

# ---------------------------------------------------------------------------
# 5. Quality gate thresholds
# ---------------------------------------------------------------------------
# Per-post character budget; also bounds keyword-extraction cost.
TEXT_PREVIEW_LENGTH = 10000

# Structural checks, each targeting one distinct class of low-value input.
MIN_CONTENT_CHARS = 180
MIN_WORD_COUNT = 30
MIN_ALPHA_RATIO = 0.55         # Rejects binary, base64 and markup residue
MAX_HASHTAG_RATIO = 0.25       # Rejects hashtag-only posts
MIN_UNIQUE_TOKEN_RATIO = 0.35  # Rejects repetition spam
# Fixed sample window keeps the diversity test length-independent: vocabulary
# richness naturally declines as a document grows (Heaps' law).
REPETITION_WINDOW = 200
LANG_MIN_PROB = 0.85

# Bot walls, paywalls and error pages that servers still return with HTTP 200.
JUNK_PATTERNS = [
    'enable javascript', 'just a moment', 'are you a robot', 'access denied',
    'subscribe to continue', '403 forbidden', 'captcha', 'verify you are human',
    'page not found', 'your browser is out of date',
]

# ---------------------------------------------------------------------------
# 6. Sampling
# ---------------------------------------------------------------------------
# Collectors aim for this; prune_database() keeps exactly this many per platform.
TARGET_POSTS_PER_PLATFORM = 25

# Manual searches get full depth; background cycles trade depth for speed.
MANUAL_MAX_POSTS = TARGET_POSTS_PER_PLATFORM
BACKGROUND_MAX_POSTS = 5
# (per-platform floor, scan total floor). Reduced scans keep any platform with
# at least one valid post so low-volume communities still appear on the homepage.
NOISE_FLOORS_FULL = (5, 15)
NOISE_FLOORS_REDUCED = (1, 6)
REDUCED_SCAN_MAX_POSTS = 5
# Hacker News minimum story points by scan depth. Kept low so niche topics are not
# starved; the gate and per-platform Z-scores still separate weak from strong.
HN_MIN_POINTS_REDUCED = 2
HN_MIN_POINTS_FULL = 5

# The gate rejects many raw results, so the candidate pool oversamples the target.
CANDIDATE_OVERSAMPLE = 4
MAX_POSTS_PER_PLATFORM = TARGET_POSTS_PER_PLATFORM * CANDIDATE_OVERSAMPLE

# First deep-fetch wave; later waves adapt to the observed acceptance rate.
COLLECTION_BATCH_SIZE = 30

# ---------------------------------------------------------------------------
# 7. Rate limiting
# ---------------------------------------------------------------------------
# Per-host asyncio semaphore sizes, keeping each wave inside API rate limits.
MAX_CONCURRENT_GITHUB = 5
MAX_CONCURRENT_DEVTO = 2     # Dev.to enforces a stricter per-client limit
MAX_CONCURRENT_SCRAPES = 8   # External pages linked from Hacker News (many hosts)

# GitHub quota reserve (60 req/h unauthenticated, 5000 with GITHUB_TOKEN): below
# this floor the collector stops spending quota on README fallbacks.
GITHUB_RATE_LIMIT_FLOOR = 15

# Per-request and per-platform timeouts; a stalled platform yields zero posts
# instead of holding the scan open.
HTTP_TIMEOUT_S = 15.0
PLATFORM_TIMEOUT_S = 150

# ---------------------------------------------------------------------------
# 8. Background worker (scheduler.py)
# ---------------------------------------------------------------------------
SCHEDULER_INTERVAL_HOURS = float(os.environ.get("SCHEDULER_INTERVAL_HOURS", "2"))
# Delay before the first cycle (0 = immediately; the dashboard shows a cold-start screen).
SCHEDULER_FIRST_RUN_DELAY_S = int(os.environ.get("SCHEDULER_FIRST_RUN_DELAY_S", "0"))
# How long a background scan waits for a previous background scan to finish.
SCHEDULER_LOCK_WAIT_S = 600

# Base delay of the cold-start top-up (linear backoff per retry).
COLD_START_RETRY_S = 2

# Discovery phase: seed topics come from today's Hacker News and Dev.to front pages.
SEED_TOPIC_COUNT = 5
HN_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
DEVTO_ARTICLES_URL = "https://dev.to/api/articles"
DISCOVERY_HN_STORIES = 40
DISCOVERY_DEVTO_ARTICLES = 60
DISCOVERY_TIMEOUT_S = 10.0
# A tag must appear on at least this many fetched articles to be a candidate.
DISCOVERY_MIN_TAG_COUNT = 2
# Community/meta tags that describe a format or event rather than a technology.
DISCOVERY_TAG_BLOCKLIST = {
    'beginners', 'programming', 'webdev', 'tutorial', 'discuss', 'productivity',
    'career', 'news', 'watercooler', 'showdev', 'devchallenge', 'challenge',
    'learning', 'codenewbie', 'help', 'opensource', 'development', 'coding',
    'welcome', 'top7', 'weeklyretro', 'devjournal', 'buildinpublic', 'writing',
    'hacktoberfest', 'community', 'meta', 'software', 'tech', 'technology',
}

# TTL: older rows are deleted at the start of every background cycle.
DATA_RETENTION_DAYS = 7

# ---------------------------------------------------------------------------
# 9. Momentum rule & community affinity
# ---------------------------------------------------------------------------
# Strong momentum (🔥) = spread (posts on >= HOT_MIN_PLATFORMS communities) and
# volume (>= HOT_MIN_TOTAL_POSTS posts). Counts, not Z-scores, because Z-scores are
# relative to each topic's own sample. Background scans cap at 20 posts per topic.
HOT_MIN_PLATFORMS = 3
HOT_MIN_TOTAL_POSTS = 12

# Community affinity (engine/sentiment.py): VADER compound polarity in [-1, 1].
SENTIMENT_POST_THRESHOLD = 0.05
SENTIMENT_HIGHLY_POSITIVE = 0.35   # mean compound at or above -> "highly positive"
SENTIMENT_MIXED_SHARE = 0.30       # both shares at or above -> "mixed"
SENTIMENT_TEXT_CHARS = 300         # title + this much body text is scored
