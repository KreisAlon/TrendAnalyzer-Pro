# config.py

# ---------------------------------------------------------------------------
# Agentic topic targeting
# ---------------------------------------------------------------------------
# The pipeline is no longer hardwired to "AI". It is pointed at a high-level topic
# and DynamicTopicManager (main.py) asks Gemini to expand that topic into the exact
# per-platform queries and the two keyword tiers for the run. Everything in this
# section is the FALLBACK: the curated plan used when no key is configured, the SDK
# is missing, or the model returns something unusable. Losing the planner therefore
# costs adaptivity, never the cycle.

# Overridable at launch: `python main.py "AI agent workflows"`, or TARGET_TOPIC in
# the environment. Kept here so the dashboard and the engine read one default.
import os as _os

TARGET_TOPIC = _os.environ.get("TARGET_TOPIC", "AI and machine learning")

# Gemini model preference order. The first that constructs is used; the rest exist
# because model aliases are retired on Google's schedule, not ours, and a retired
# alias raised on construction and took the whole feature down.
GEMINI_MODELS = (
    "models/gemini-3.8-flash",
    "models/gemini-flash-latest",
    "models/gemini-2.5-flash",
    "models/gemini-2.0-flash",
)

# Seconds before a planning call is abandoned and the curated fallback is used.
# Ingestion must not sit behind a hanging planner: this is the first thing a cycle
# does, so a stalled call would delay every collector behind it.
TOPIC_PLAN_TIMEOUT = 45

# --- Per-platform fallback query plans ---
# Each platform's search API takes a different shape, which is why the planner is
# asked for four separate lists rather than one set of "search queries":
#   GitHub      topic slugs   (repository search has NO boolean OR; see github.py)
#   Hacker News free text     (Algolia full-text index)
#   Dev.to      single tags   (the /articles endpoint filters on one tag per call)
#   Mastodon    hashtags      (tag timelines, no spaces or hyphens)
DEFAULT_GITHUB_TOPICS = [
    'llm', 'machine-learning', 'ai-agents', 'generative-ai',
    'deep-learning', 'nlp', 'rag',
]
DEFAULT_HN_QUERIES = ["LLM", "AI model", "GPT", "machine learning", "AI agents"]
DEFAULT_DEVTO_TAGS = ["ai", "machinelearning", "llm"]
DEFAULT_MASTODON_TAGS = ["ai", "llm", "machinelearning", "genai", "generativeai"]

# --- Noise Filtering Keywords (The Gatekeeper) ---
# Split into two tiers because short ambiguous tokens were the source of nearly all
# false positives: a naive substring test for 'ai' matches "IsaacLab", "available" and
# "Snowden archive", and 'ml' matches "html". Matching is done on WORD BOUNDARIES
# (see BaseCollector.is_topic_relevant), and the weak tier needs corroboration.
# These two lists are now the FALLBACK vocabulary: DynamicTopicManager replaces them
# per run with tiers generated for the target topic, applied through
# BaseCollector.apply_topic_profile(). The tier SEMANTICS never change.

# Unambiguous AI terms. A single hit anywhere is enough to accept a post.
AI_STRONG_KEYWORDS = [
    'llm', 'gpt', 'machine learning', 'deep learning', 'neural network',
    'neural networks', 'transformer', 'transformers', 'pytorch', 'tensorflow',
    'openai', 'anthropic', 'diffusion model', 'stable diffusion', 'rag',
    'fine-tuning', 'fine tuning', 'huggingface', 'hugging face', 'deepseek',
    'embeddings', 'generative ai', 'agentic', 'inference', 'gemini', 'llama',
]

# Ambiguous on their own ('agent' appears in unrelated software writing, 'vector' is a
# plain maths term). Accepted only in a title, or when two DISTINCT weak terms co-occur.
AI_WEAK_KEYWORDS = [
    'ai', 'ml', 'nlp', 'agent', 'agents', 'automation', 'vector',
    'data science', 'claude', 'neural', 'model', 'models', 'training',
]

# Preserved as the union so any existing importer keeps working.
AI_FILTER_KEYWORDS = AI_STRONG_KEYWORDS + AI_WEAK_KEYWORDS

# --- Content Processing Settings ---
# Determines the amount of text extracted for semantic analysis. Every collector cuts
# its deep-fetched body at this budget through BaseCollector.truncate_at_boundary(),
# which backs up to the last sentence end so a stored post never stops mid-word.
TEXT_PREVIEW_LENGTH = 10000

# --- Quality Gate Thresholds ---
# Tuned against the 100 rows previously stored: the worst offender was a 42-character
# post consisting entirely of hashtags ("# AI # BigTech # Musk # Meta # SocialMedia").
MIN_CONTENT_CHARS = 180     # Reject stubs that give KeyBERT nothing to work with
MIN_WORD_COUNT = 30         # Guards against long strings of a few repeated tokens
MIN_ALPHA_RATIO = 0.55      # Letters / total chars. Kills binary, base64 and nav junk
MAX_HASHTAG_RATIO = 0.25    # Fraction of tokens that are '#tags'. Kills hashtag soup
MIN_UNIQUE_TOKEN_RATIO = 0.35  # Unique / total words. Kills repetition spam
REPETITION_WINDOW = 200     # Words sampled for the ratio above, so the test is
                            # length-independent (vocabulary richness drops with length)
LANG_MIN_PROB = 0.85        # langdetect confidence floor for accepting English

# Bot walls and paywalls that a scraper happily returns with HTTP 200. Matched
# case-insensitively against the first part of the extracted text.
JUNK_PATTERNS = [
    'enable javascript', 'just a moment', 'are you a robot', 'access denied',
    'subscribe to continue', '403 forbidden', 'captcha', 'verify you are human',
    'page not found', 'your browser is out of date',
]

# --- Collector Configuration ---
COLLECTORS_CONFIG = {
    'per_page': 100,
    'max_items': 200
}

# How many posts each platform must end the cycle with. prune_database() keeps exactly
# this many rows per platform, so a collector that returns fewer leaves the dashboard
# unbalanced -- GitHub and Mastodon were finishing at 10 and 8.
TARGET_POSTS_PER_PLATFORM = 25

# Candidates fetched per post we actually need. The gatekeeper (relevance, language,
# length, duplicates) rejects roughly half to three quarters of what the search APIs
# return, so asking for exactly 25 candidates can never yield 25 stored posts.
CANDIDATE_OVERSAMPLE = 4

# Upper bound on candidates considered per platform. Collectors walk this pool in
# concurrent waves and stop as soon as TARGET_POSTS_PER_PLATFORM have cleared the
# gate, so the ceiling costs nothing when acceptance is high.
MAX_POSTS_PER_PLATFORM = TARGET_POSTS_PER_PLATFORM * CANDIDATE_OVERSAMPLE

# Candidates deep-fetched per wave on the first pass. Later waves are sized from the
# acceptance rate measured so far.
COLLECTION_BATCH_SIZE = 30

# --- Rate Limiting ---
# Caps on simultaneous deep-fetch requests per host. Without these, a cycle fires a
# whole wave of parallel requests at a single API and risks a 429.
MAX_CONCURRENT_GITHUB = 5
MAX_CONCURRENT_DEVTO = 2
MAX_CONCURRENT_SCRAPES = 8

# GitHub's unauthenticated core limit is 60 req/hr, and one cycle wants ~50 READMEs.
# Below this remaining budget the collector degrades to description-only instead of
# burning the quota. Set a GITHUB_TOKEN in the environment to raise the ceiling to 5000/hr.
GITHUB_RATE_LIMIT_FLOOR = 15
