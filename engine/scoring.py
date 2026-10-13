"""
Persistence and scoring of a completed cycle: the interactive path stores the
user's corpus; the background path scores in a scratch DB and keeps a summary.
"""

import os
import tempfile

from database.manager import TrendManager
from engine.planning import current_topic
from engine.signals import _platform_buzzwords, _platform_sentiment


def _store_and_score(topic, profile, collectors, all_posts, result):
    """Interactive path: store as the user's corpus, normalise, prune, record volume."""
    db_manager = TrendManager()
    # A new topic replaces the corpus; a repeat scan of the same topic extends it.
    previous = current_topic()
    result.replaced_previous_topic = (previous or '').strip().lower() != topic.lower()
    if result.replaced_previous_topic:
        # Atomic swap: a failure leaves the previous corpus untouched.
        result.saved = db_manager.replace_posts(all_posts)
    else:
        result.saved = db_manager.save_posts(all_posts)

    # Per-platform normalisation (engagement units differ), then stratified Top-K.
    for collector in collectors:
        collector.recalculate_platform_stats(db_manager.db_path)
    db_manager.prune_database()
    # Saved only with a corpus, so on-screen figures always describe on-screen posts.
    db_manager.save_volume(topic, list(result.volume.values()))

    # Last, so the topic label changes only once the corpus belongs to it.
    profile.save()


def _score_background(topic, collectors, all_posts, result, max_posts):
    """
    Background path: normalise in a throwaway DB, then record per-platform scores,
    matches, buzzwords, sentiment and top posts in live_trends / live_posts.
    """
    fd, scratch_path = tempfile.mkstemp(prefix="trend_bg_", suffix=".db")
    os.close(fd)
    try:
        scratch = TrendManager(db_path=scratch_path)
        result.saved = scratch.save_posts(all_posts)
        # Same normalisation as an interactive scan, over this topic only.
        for collector in collectors:
            collector.recalculate_platform_stats(scratch_path)
        scratch.prune_database(max_per_platform=max_posts)
        scores = scratch.platform_scores()
        # Every retained post: the homepage ranks the full collection.
        top_posts = scratch.top_posts(per_platform=max_posts)
    finally:
        # WAL mode leaves -wal/-shm side files next to the scratch database.
        for path in (scratch_path, f"{scratch_path}-wal", f"{scratch_path}-shm"):
            try:
                os.remove(path)
            except OSError:
                pass

    TrendManager().save_live_trend(
        topic, scores, keywords=_platform_buzzwords(all_posts), top_posts=top_posts,
        sentiment=_platform_sentiment(all_posts),
        matches={p: r.get('estimated_matches') for p, r in result.volume.items()})
