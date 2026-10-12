"""
Ingestion planning: turns a validated topic into the collectors' search plan and
reports which topic the stored corpus belongs to.
"""

from datetime import datetime, timezone

from engine.topic_profile import TopicProfile


class DynamicTopicManager:
    """
    Converts a topic into a search plan. Deterministic: built from the topic's own
    words, so the same topic always yields the same plan.
    """

    def __init__(self, target_topic):
        self.target_topic = target_topic.strip()

    def resolve(self):
        """Returns the TopicProfile for this topic, stamped with its creation time."""
        profile = self._plan()
        profile.generated_at = datetime.now(timezone.utc).isoformat()
        return profile

    def _plan(self):
        """Builds a keyword-search plan (source "keywords" or "fallback")."""
        return TopicProfile.from_search_terms(self.target_topic)


def current_topic():
    """Returns the stored corpus's topic, or None before the first scan."""
    profile = TopicProfile.load()
    return profile.target_topic if profile else None
