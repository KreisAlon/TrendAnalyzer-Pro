"""Topic validation: the input contract enforced before any network or DB work."""

import re

# Upper bound on accepted topic length; also bounds every derived query.
MAX_TOPIC_CHARS = 80


def normalise_topic(text):
    """Collapses whitespace and enforces the length bound; '' when nothing remains."""
    # The trailing strip removes a space left by the length cut.
    return re.sub(r'\s+', ' ', str(text or '')).strip()[:MAX_TOPIC_CHARS].strip()


# English letters, digits, spaces and tech-name punctuation ("c++", "node.js", "c#",
# "ci/cd"). All sources are English, so other scripts could never match.
_ENGLISH_TOPIC_RE = re.compile(r"^[A-Za-z0-9 .,+#&'/()\-:]+$")

ENGLISH_ONLY_MESSAGE = "Please enter English terms only."


def validate_topic(text):
    """
    Validates a raw search (called by the dashboard and again inside `scan()`).

    Returns:
        tuple[str, str]: (topic, error); exactly one is non-empty.
    """
    topic = normalise_topic(text)
    if not topic:
        return "", "Type a topic to scan for."
    if not _ENGLISH_TOPIC_RE.match(topic):
        return "", ENGLISH_ONLY_MESSAGE
    # Punctuation or digits alone carry no searchable meaning.
    if not re.search(r'[A-Za-z]', topic):
        return "", "Please include at least one word."
    return topic, ""
