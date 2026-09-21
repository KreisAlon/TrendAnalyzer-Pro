# config.py

# --- Noise Filtering Keywords (The Gatekeeper) ---
# Split into two tiers because short ambiguous tokens were the source of nearly all
# false positives: a naive substring test for 'ai' matches "IsaacLab", "available" and
# "Snowden archive", and 'ml' matches "html". Matching is done on WORD BOUNDARIES
# (see BaseCollector.is_ai_relevant), and the weak tier needs corroboration.

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
# Determines the amount of text extracted for semantic analysis.
TEXT_PREVIEW_LENGTH = 1500

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
    'per_page': 50,
    'max_items': 100
}

MAX_POSTS_PER_PLATFORM = 50

# --- Rate Limiting ---
# Caps on simultaneous deep-fetch requests per host. Without these, a cycle fires 50
# parallel requests at a single API and risks a 429.
MAX_CONCURRENT_GITHUB = 5
MAX_CONCURRENT_DEVTO = 5
MAX_CONCURRENT_SCRAPES = 8

# GitHub's unauthenticated core limit is 60 req/hr, and one cycle wants ~50 READMEs.
# Below this remaining budget the collector degrades to description-only instead of
# burning the quota. Set a GITHUB_TOKEN in the environment to raise the ceiling to 5000/hr.
GITHUB_RATE_LIMIT_FLOOR = 15
